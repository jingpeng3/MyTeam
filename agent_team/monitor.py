"""监控：扫在途任务的 outbox 与 pane 存活，推进 DAG（完成 / 失败重试 / 超时 / 等人 / 打回）。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import herdr, signals, ledger, pane_scan, dispatch, reclaim, interventions
from .board import Board
from .config import Paths
from .models import (Task, Result, DISPATCHED, DONE, FAILED, PENDING,
                     AWAITING_INPUT, REJECT, DRY_PANE, runtime_key)

# dispatched 任务无 outbox 的默认超时(秒)；task.timeout_s>0 时以任务为准。给足余量，
# 只用于兜住"worker 静默死亡"，不影响正常执行。
DEFAULT_TASK_TIMEOUT = 900
# worker 本轮 turn 已结束(idle/done)却没写 outbox 的宽限：超过即判「交付缺失」快速失败，
# 不再白等满 DEFAULT_TASK_TIMEOUT。给几秒是为躲开「turn 刚结束、outbox 正在落盘」的竞态。
IDLE_NO_OUTBOX_GRACE = 10
# 任务从首次分派起的**绝对存活上限**倍数：即便 herdr 一直报 alive（live-but-stuck：死循环/
# 卡在工具/等待输入但仍被判 working），超过 factor×有效超时也强制判失败并回收，
# 防止心跳把墙钟无限推后、让挂死 worker 永久占用全局 agent 容量。
MAX_LIFETIME_FACTOR = 3
# 进入 AWAITING_INPUT（等人拍板）后的**存活上限**(秒)：等人期间暂停 max_lifetime_s 硬上限，
# 仅受本上限约束；超过仍判失败回收，防被遗忘的「等人」任务永久占容量。task.awaiting_timeout_s 可覆写。
AWAITING_INPUT_TIMEOUT = 3600


def _mark_retry_or_failed(task: Task) -> None:
    """失败任务：未超重试上限则回 PENDING 并清 agent（可换人重试），否则 FAILED。"""
    if task.attempts > task.max_retries:
        task.status = FAILED
    else:
        task.status = PENDING
        task.agent = ""          # 清掉，让 select_agent 有机会换一个 eligible agent


def log_new_skips(paths: Paths, board: Board, skipped_ids: list[str]) -> None:
    """级联 SKIPPED 本身不产生任何事件——排障时看不到「因某失败连带跳过了谁」。
    这里对本轮新跳过的节点各补一条事件，并透传根因 task_id，深层级联一眼见源头。"""
    for tid in skipped_ids:
        t = board.tasks.get(tid)
        if t is None:
            continue
        ledger.log_event(paths, "task_skipped", goal_id=board.goal_id, task_id=tid,
                         blocked_by=board.blockers(t), root=board.root_failure(t))


def flush_skips(paths: Paths, board: Board) -> None:
    """分派收尾：refresh_ready → 若有新级联 SKIPPED 则记事件并落盘。

    让「分派期失败（如 provisioning 耗尽）」触发的级联跳过也能持久化并可观测，
    否则会出现 run 报完成、磁盘却停在 pending 发散。tick 与 tick_all 共用。
    """
    newly = board.refresh_ready()
    if newly:
        log_new_skips(paths, board, newly)
        board.save(paths)


@dataclass
class _Verdict:
    """无 outbox 时对一个在途任务的判定。fail_reason=None 表示仍在正常运行/等人、本轮不收尾。"""
    fail_reason: str | None = None
    terminal: bool = False        # 终态失败、不重试（致命信号 / 交付损坏）
    malformed: bool = False       # 交付损坏：收尾时隔离 outbox 取证而非删除
    rt_dirty: bool = False        # 账本条目被更新（心跳 / idle 锚点 / 等人标记）
    changed: bool = False         # 状态有变化（进入或退出等人）但未收尾


def _on_alive(paths: Paths, gid: str, task: Task, info: dict, hname: str,
              now: float, prev_agent: str, v: _Verdict) -> bool:
    """herdr 报存活(working/busy/…)：扫致命信号；否则刷新心跳，若刚从等人恢复则退回 dispatched。
    返回是否刚从等人恢复（恢复即重置存活时钟，调用方据此重算硬上限）。"""
    # herdr 报 working 不等于在推进：先扫致命/停滞信号（auth/密级/额度/重试耗尽），
    # 命中即终态失败、不重试——否则这类「活着的死循环」要等 45min 绝对上限才被杀。
    fatal = pane_scan.fatal_reason(pane_scan.read_tail(hname, live=True))
    if fatal:
        v.fail_reason, v.terminal = f"fatal: {fatal}", True
        return False
    resumed = task.status == AWAITING_INPUT
    if resumed:
        # 之前在等人、现在又 working（人已答复、worker 恢复执行）→ 退回 dispatched 继续跑
        task.status = DISPATCHED
        task.mark_resumed(now)        # 等人时段计入 awaiting_s、不算净执行
        info.pop("awaiting_since", None)
        info.pop("awaiting_reason", None)
        # 重置存活时钟：等人期间豁免了硬上限，恢复后按新任务给足预算，
        # 否则原 dispatched_at 派生的硬上限可能已过、一恢复就被误杀。
        info["dispatched_at"] = now
        ledger.log_event(paths, "task_input_resumed", goal_id=gid,
                         task_id=task.task_id, agent=prev_agent)
        v.changed = True
    info["last_alive_at"] = now   # 心跳：活着且在产出，刷新存活时刻
    info["idle_since"] = None      # 重新活跃，清空 idle 锚点
    v.rt_dirty = True
    return resumed


def _awaiting_timeout_reason(task: Task, info: dict, now: float) -> str | None:
    """等人任务的唯一超时判据：now - awaiting_since 超过 awaiting 上限才判失败，否则 None。
    缺 awaiting_since 时以本轮为锚点补上（调用方负责标 rt_dirty 落盘）。"""
    aw_since = info.setdefault("awaiting_since", now)
    aw_timeout = task.awaiting_timeout_s or AWAITING_INPUT_TIMEOUT
    if now - aw_since > aw_timeout:
        return f"awaiting human input timeout {int(now - aw_since)}s (> {aw_timeout}s)"
    return None


def _on_ready(paths: Paths, gid: str, task: Task, info: dict, hname: str, st: str,
              ostate: str, started: float, now: float, prev_agent: str, v: _Verdict) -> None:
    """herdr 报 idle/done（turn 已结束）且无可用 outbox：致命信号 → 等人提问 → 宽限后判交付缺失/损坏。"""
    # 先扫致命/停滞信号——worker 报 auth/额度/密级等致命错误后常常**就此结束回合(idle/done)**，
    # 此时签名清楚摆在 pane 里却因状态门控被当「交付缺失」白重试一轮（撞同一堵墙、可能重复非幂等副作用）。
    # 与 alive 分支对齐：命中即终态失败、不重试。
    tail = pane_scan.read_tail(hname)     # 读一次 pane，致命与等人判定共用
    fatal = pane_scan.fatal_reason(tail)
    if fatal:
        v.fail_reason, v.terminal = f"fatal: {fatal}", True
        return
    question = pane_scan.awaiting_reason(tail)
    v.rt_dirty = True
    if question:
        if task.status != AWAITING_INPUT:
            task.status = AWAITING_INPUT
            task.mark_awaiting(now)
            info["awaiting_since"] = now
            ledger.log_event(paths, "task_awaiting_input", goal_id=gid,
                             task_id=task.task_id, agent=prev_agent, question=question)
            v.changed = True
        info["awaiting_reason"] = question
        # 有界上限：等人也不能无限占容量，超过 awaiting 上限仍判失败回收
        v.fail_reason = _awaiting_timeout_reason(task, info, now)
        return
    idle_since = info.get("idle_since") or now
    info["idle_since"] = idle_since
    if now - idle_since <= IDLE_NO_OUTBOX_GRACE:
        return
    if ostate == "malformed":
        # worker 已交付、只是打捞后仍无法解析：重试通常既徒劳(同 worker 大概率再产坏输出)
        # 又重复非幂等副作用 → 判终态失败，而非和 missing 一样机械重试。
        v.fail_reason, v.terminal, v.malformed = "outbox malformed (delivery corrupt)", True, True
    elif info.get("last_alive_at", started) <= started:
        # 从未被观测到「working」就已 idle 且无 outbox：worker 会话根本没在执行
        # （被删/被停/backend 没跑 turn），区别于「跑过但没落信号」，让日志一眼看出「没启动」。
        v.fail_reason = f"agent {st}, never started executing (worker session not running?)"
    else:
        v.fail_reason = f"agent {st} but no outbox (delivery missing)"


def _judge_no_outbox(paths: Paths, gid: str, task: Task, info: dict, ostate: str,
                     now: float, prev_agent: str) -> _Verdict:
    """无可用 outbox 时的兜底判定（先存活、后绝对上限、再墙钟），见 monitor_once 文档。会就地更新 info。"""
    v = _Verdict()
    started = info.get("dispatched_at", now)          # 首次分派，永不刷新（等人恢复时重置）
    last_alive = info.get("last_alive_at", started)   # 心跳刷新
    hname = info.get("herdr_name")
    eff_timeout = task.timeout_s or DEFAULT_TASK_TIMEOUT
    # 绝对存活上限与交付超时**解耦**：可用 task.max_lifetime_s 独立覆写，否则按 factor 派生。
    max_lifetime = task.max_lifetime_s or eff_timeout * MAX_LIFETIME_FACTOR
    if not info:                              # ledger-lost：账本无此条目
        v.fail_reason = "ledger-lost: no runtime entry"
        return v
    if info.get("pane_id") == DRY_PANE or not hname:
        # dry-run 幽灵 / 从未真起过 pane：无存活可查，快速失败清理，不落墙钟
        v.fail_reason = "dry-run/no-pane ghost entry (never really dispatched)"
        return v
    try:
        st = herdr.agent_status(hname)
    except herdr.HerdrError:
        st = "unknown"
    if st in herdr.DEAD_STATES:
        v.fail_reason = f"agent {st}"
    elif st in herdr.ALIVE_STATES:
        if _on_alive(paths, gid, task, info, hname, now, prev_agent, v):
            started = now     # 刚恢复：硬上限按新时钟算，否则本轮底部守卫会把刚恢复的任务当场误杀
    elif st in herdr.READY_STATES:
        _on_ready(paths, gid, task, info, hname, st, ostate, started, now, prev_agent, v)
    elif task.status == AWAITING_INPUT:
        # unknown + 等人：pane 本就静默、心跳不会刷新，不能按 last_alive 陈旧度判死——
        # 否则一次 herdr 抖动就关 pane、白耗重试、丢掉待答问题。只按 awaiting 上限判。
        v.rt_dirty = "awaiting_since" not in info
        v.fail_reason = _awaiting_timeout_reason(task, info, now)
    elif now - last_alive > eff_timeout:
        # unknown/无法归类：用心跳陈旧度判「疑似静默死亡」，快速回收，
        # 不再干等绝对上限（未设 timeout_s 时可达 2700s，会长期霸占全局 agent 容量）。
        v.fail_reason = f"agent {st}, silent {int(now - last_alive)}s (suspected dead)"
    # 绝对上限：即便 alive，超过硬上限也必须失败回收（live-but-stuck 兜底）。
    # **AWAITING_INPUT 豁免**——等人期间暂停硬上限，改由 awaiting 上限约束。
    if v.fail_reason is None and task.status != AWAITING_INPUT and now > started + max_lifetime:
        v.fail_reason = f"exceeded max lifetime {int(now - started)}s (> {int(max_lifetime)}s)"
    return v


def _fail_inflight(paths: Paths, gid: str, task: Task, v: _Verdict, now: float,
                   prev_agent: str) -> None:
    """无 outbox 判定为失败：收尾计时，终态失败或按重试预算退回待派，记事件。"""
    fatal = v.fail_reason.startswith("fatal:")
    task.error = f"dispatch {v.fail_reason}"
    task.close_run("fatal" if fatal else "timeout", now)
    if v.terminal:
        task.status = FAILED
    else:
        _mark_retry_or_failed(task)
    # fatal:（致命/停滞信号）单独记 task_fatal，让「流程问题」在日志/报告里一眼可见，
    # 区别于普通交付超时/静默死亡。
    ledger.log_event(paths, "task_fatal" if fatal else "task_timeout", goal_id=gid,
                     task_id=task.task_id, agent=prev_agent, reason=v.fail_reason,
                     new_status=task.status,
                     no_alternate_agent=dispatch.all_eligible_tried(paths, task))


def _apply_done(paths: Paths, gid: str, task: Task, res: Result, now: float,
                prev_agent: str) -> None:
    task.status = DONE
    task.close_run("done", now, res.written_at)
    task.result_summary = res.summary
    task.artifacts = res.artifacts
    task.artifact_warnings = _missing_artifacts(res.artifacts)
    task.error = res.error
    if task.artifact_warnings:
        # 交付「说写了、实际没落盘」：不判失败（见 _missing_artifacts 的理由），
        # 但记事件 + surface 给协调者，别等下游读到空文件才炸。
        ledger.log_event(paths, "task_artifacts_missing", goal_id=gid,
                         task_id=task.task_id, agent=prev_agent,
                         missing=task.artifact_warnings)
    ledger.log_event(paths, "task_done", goal_id=gid, task_id=task.task_id, agent=prev_agent)


def _apply_failed(paths: Paths, gid: str, task: Task, res: Result, now: float,
                  prev_agent: str) -> None:
    # worker 显式声明 retryable=false（永久失败）→ 直接判 FAILED，跳过无谓重试
    # （flow-25：注入/契约类失败重试必然复现，白耗一轮 pane 拉起 + interval）。
    task.close_run("failed", now, res.written_at)
    if res.retryable is False:
        task.status = FAILED
    else:
        _mark_retry_or_failed(task)
    task.result_summary = res.summary
    task.artifacts = res.artifacts
    task.error = res.error
    # no_alternate_agent 对 PENDING/FAILED 两种结局都应成立，判据是「全部 eligible 都已试过」
    # ——才是「换人已无意义」的真正条件。
    ledger.log_event(paths, "task_failed", goal_id=gid, task_id=task.task_id,
                     agent=prev_agent, new_status=task.status, retryable=res.retryable,
                     no_alternate_agent=dispatch.all_eligible_tried(paths, task))


def _apply_reject(paths: Paths, board: Board, rt: dict, task: Task, res: Result,
                  now: float) -> dict:
    """评审/校验节点打回上游：把目标 + 其全部后代（含本打回者）重置为待派重跑，并把本次意见+证据作为
    凭证累积注入目标；达打回上限则升级为 AWAITING_INPUT 交协调者裁决。

    打回请求无效（目标不存在 / 不在声明的 reject_to 里 / 是 BYPASSED|SKIPPED 终态）时，别让打回者
    永远卡在途——按普通完成收尾（它确实跑完了），返回值带 invalid=True，由调用方照常回收。"""
    # 打回者本次确实跑完了：先按 reject 收尾计时，否则下面的连锁重置会把它记成 reset。
    run = task.close_run("reject", now, res.written_at)
    rej = interventions.apply_reject_core(
        paths, board, rt, res.reject_to or "",
        from_task=task.task_id, reason=res.summary or "下游打回（未附意见）",
        evidence=res.artifacts, enforce_declared=True, force=False)
    rej["invalid"] = bool(rej.get("not_found") or rej.get("rejected_illegal")
                          or rej.get("rejected_terminal"))
    if rej["invalid"]:
        ledger.log_event(paths, "reject_invalid", goal_id=board.goal_id, task_id=task.task_id,
                         target=res.reject_to,
                         reason="target 不存在 / 不在 reject_to 声明内 / 是 bypassed|skipped 终态")
        task.status = DONE
        if run is not None:
            run["outcome"] = "reject_invalid"
        task.result_summary = res.summary
        task.artifacts = res.artifacts
        task.error = f"打回目标非法({res.reject_to!r})，已按完成处理"
    return rej


def monitor_once(paths: Paths, board: Board, close_panes: bool = True) -> list[str]:
    """扫一遍 dispatched 任务的 outbox，推进 DAG；返回本轮有状态变化的 task_id。

    无 outbox 时的兜底判定（先存活、后绝对上限、再墙钟）——既不误杀慢 worker，也不让挂死 worker 永生：
    - dry-run 幽灵条目(pane_id=<dry-run>、无 herdr_name) → 立即清理，绝不落墙钟白等 900s。
    - agent 死亡(error/crashed/gone/dead) → 立即判失败。
    - agent 存活(working/busy/…) → 刷新 `last_alive_at` 心跳，清 idle_since；不误杀。
    - agent 已就绪(idle/done) → 记 `idle_since` 锚点，超过 grace 才判「交付缺失」（独立锚点，
      不与心跳/interval 耦合，避免 turn 刚结束、outbox 正在落盘就被误杀）。
    - **绝对存活上限**：now - dispatched_at(不可变) > factor×有效超时 → 无论是否 alive 都判失败并回收，
      防心跳把墙钟无限推后、live-but-stuck 永久占用容量。
    - 账本无此 dispatched 条目(ledger-lost) → 判失败重发。
    """
    rt = ledger.load(paths)
    original = ledger.snapshot(rt)     # 收尾按锁内合并提交，保住并发驱动者(如后台 run)期间新增的分派条目
    gid = board.goal_id
    changed: list[str] = []
    rt_dirty = False
    reset_by_reject: set[str] = set()   # 本轮因打回被重置为待派的节点：跳过其后续复查，避免误判 ledger-lost
    for task in board.pollable_tasks():          # dispatched + awaiting_input：等人的也要每轮复查恢复/收尾
        now = time.time()                 # 每任务重取，避免跨多次阻塞 agent_status 调用后时钟漂移
        rkey = runtime_key(gid, task.task_id)
        info = rt.get(rkey, {})
        if task.task_id in reset_by_reject:
            continue                      # 本轮已被打回重置（回到 PENDING）——它已不是在途任务，别再当 dispatched 复查
        if task.status == AWAITING_INPUT and info.get("escalated"):
            continue                      # 打回已升级、parked 等协调者裁决：暂停一切存活/超时判定，直到 CLI 介入
        # since=本次派发时刻：仅用于「裸名信号」的宽容采纳——只认本次派发之后写的裸名文件，
        # 避免把 outbox 目录里同名历史任务的陈旧残留当成本次交付。
        res, ostate = signals.read_outbox_ex(paths, task.agent, gid, task.task_id,
                                             since=info.get("dispatched_at"))
        prev_agent = info.get("agent") or task.agent   # 清 agent 前先存好，供日志/回收/清信号
        quarantine = False
        if res is None:
            v = _judge_no_outbox(paths, gid, task, info, ostate, now, prev_agent)
            if v.rt_dirty:
                rt[rkey] = info
                rt_dirty = True
            if v.fail_reason is None:
                if v.changed:
                    changed.append(task.task_id)
                continue                              # 仍在正常运行/等人，下轮再看
            _fail_inflight(paths, gid, task, v, now, prev_agent)
            quarantine = v.malformed                  # 判 malformed 时隔离 outbox 取证，不直接删
        elif res.status == REJECT:
            rej = _apply_reject(paths, board, rt, task, res, now)
            rt_dirty = rt_dirty or rej.get("rt_dirty", False)
            if not rej["invalid"]:
                # 有效打回（正常回退 or 升级）：核心已按需回收 pane/信号/workspace 并重置相关节点。
                changed.append(task.task_id)
                changed.extend(t for t in rej["reset"] if t != task.task_id)
                reset_by_reject.update(rej["reset"])
                continue
        elif res.status == DONE:
            _apply_done(paths, gid, task, res, now, prev_agent)
        else:
            _apply_failed(paths, gid, task, res, now, prev_agent)

        changed.append(task.task_id)
        reclaim.reclaim_task(paths, rt, gid, task.task_id, prev_agent,
                             close_pane=close_panes, quarantine=quarantine)
        rt_dirty = True
    if changed:
        log_new_skips(paths, board, board.refresh_ready())
        board.save(paths)
    if rt_dirty:
        ledger.commit_merge(paths, rt, original)
    return changed


def _missing_artifacts(arts: list[str]) -> list[str]:
    """DONE 交付里「outbox 报了、但落盘查无此文件」的产物（只看绝对路径）。

    **只警告、不判失败**，理由是硬判会凭空造出一批假失败：存在性只是个启发式——worker 可能
    报相对路径（相对谁？协调者 cwd 未必是它的 cwd）、报目录、或报本就在别处的既有文件；而真
    踩过的坑（产物 JSON 外层形态不符）压根不是存在性查得出来的，那类要靠 plan 里声明 schema。
    所以这里只当「低成本哨兵」：把可疑交付记进 task.artifact_warnings 并在 monitor 里 surface，
    由协调者按 skip/retask/reject 三杠杆处置，而不是让框架替它武断判死。
    """
    out: list[str] = []
    for p in arts or []:
        if not p or not os.path.isabs(p):
            continue                      # 相对路径无从判（基准不确定），跳过不误报
        if not Path(p).exists():
            out.append(p)
    return out
