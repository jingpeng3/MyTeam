"""协调者调度入口：一轮 tick（先 monitor 收 outbox、再分派）、跨 Job 的 tick_all、卡死判定与 run 自动循环。

具体职责拆在同包子模块里，本模块只做编排，并 re-export cli/dashboard 使用的公共 API：
  pane_scan     pane 输出扫描（致命信号 / 等人提问）
  ledger        runtime 账本读写 + run.log 事件
  bootstrap     worker 任务信号与引导语
  dispatch      选人 + herdr 拉起 + 提交
  reclaim       在途资源回收 / 账本对账 / team workspace 回收
  monitor       outbox 收割与存活/超时判定
  interventions answer / skip / retask / reject / expand
  reporting     report 与只读视图
  retro         Job 收敛后自动建复盘 Job、收建议进待办池（自我增强）
  watch         后台收割守护：协调者不在场时也收割 outbox / 判超时 / 回收 pane（从不分派）
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import dispatch, ledger, monitor, registry, retro, watch
from .board import Board, job_lock, jobs_locked
from .config import Paths
from .models import AWAITING_INPUT, PENDING, READY, runtime_key
# 公共 API re-export：cli / dashboard / 外部脚本一律经 coordinator 调用，不感知内部拆分
from .bootstrap import build_bootstrap  # noqa: F401
from .dispatch import ProvisionError, dispatch_task, select_agent  # noqa: F401
from .interventions import (MAX_REJECTS, answer_task, apply_reject, bypass_tasks,  # noqa: F401
                            expand_fanout, retask_task)
from .monitor import (AWAITING_INPUT_TIMEOUT, DEFAULT_TASK_TIMEOUT,  # noqa: F401
                      IDLE_NO_OUTBOX_GRACE, MAX_LIFETIME_FACTOR, monitor_once)
from .reclaim import close_goal_panes, reap_team_workspace, reconcile_ledger  # noqa: F401
from .reporting import (artifact_warnings, awaiting_tasks, job_timing, report,  # noqa: F401
                        retrying_tasks, timing_by_agent)

log = logging.getLogger(__name__)


# run_loop 连续「零分派+零变化+零在途」多少轮判定容量停滞/挂死。
IDLE_STREAK_LIMIT = 3


def retro_round(paths: Paths, dry_run: bool = False) -> dict[str, Any]:
    """每次 monitor 之后调：收已完成复盘的建议进待办池 + 为新收敛的 Job 建复盘 Job。

    放在 monitor 之后，是因为「Job 收敛」与「复盘交付」都是 monitor 收 outbox 时发生的，同一次调用就能接上。
    dry-run 不复盘（没有真实执行过程）。旁路能力：出错只记日志，绝不拖垮调度。"""
    if dry_run:
        return {"retro_spawned": [], "insights_new": 0}
    try:
        ing = retro.ingest(paths)
        return {"retro_spawned": retro.sweep(paths),
                "insights_new": ing["new"] + ing["reopened"]}
    except Exception:                    # noqa: BLE001 —— 复盘是旁路，失败不影响主流程
        log.exception("retro_round 失败（已忽略，不影响调度）")
        return {"retro_spawned": [], "insights_new": 0}


def _pending_ids(paths: Paths, board: Board) -> set[str]:
    """收割前的 pending 快照，供 hold_new_ready 识别「本轮收割才解锁」的节点。
    先补一次 refresh_ready（含级联跳过落盘），免得本就该 ready 的被误当成本轮新解锁。"""
    monitor.flush_skips(paths, board)
    return {t.task_id for t in board.tasks.values() if t.status == PENDING}


def _newly_ready(board: Board, pending_before: set[str]) -> set[str]:
    # 不必再 refresh_ready：monitor_once 有变化时已刷新过；无变化也就不会有新解锁
    return {t.task_id for t in board.tasks.values()
            if t.status == READY and t.task_id in pending_before}


def tick(paths: Paths, board: Board, dry_run: bool = False,
         hold_new_ready: bool = False) -> dict[str, Any]:
    """单 Job 一轮调度：**先监控收 outbox 释放容量、再（并发）分派** ready 节点。

    monitor-先-dispatch：本轮完成的任务释放的容量，在**同一轮**就被新 ready 节点填上。
    hold_new_ready=True（交互驱动用）：**本轮收割才解锁**的下游不在同轮派出，留 READY 进 `held`，
    给协调者读上游 result_summary 后 retask/skip 的窗口——否则上游被 tick 收到时下游当场就派走了，
    只能 retask --force 回收重派。重试退回的、此前已 ready 的照常派；`run` 自动循环不 hold。

    分派末尾统一 refresh_ready + 落盘 + 记 skip 事件，让「分派期失败(如 provisioning 耗尽)」
    触发的级联 SKIPPED 也能持久化并可观测（否则 run 报完成、磁盘却停在 pending 发散）。
    """
    with job_lock(paths, board.goal_id):   # 与后台 watch / 其它驱动者互斥：锁内先同步到磁盘最新态
        board.sync(paths)
        res = _tick_locked(paths, board, dry_run, hold_new_ready)
    if not dry_run and harvestable(paths, board):
        watch.ensure_running(paths)        # 有真实在途任务：确保协调者走开后也有人收割
    return res


def _tick_locked(paths: Paths, board: Board, dry_run: bool, hold_new_ready: bool) -> dict[str, Any]:
    if not dry_run:
        reconcile_ledger(paths, scope_goal_id=board.goal_id)   # 只对账自己这个 Job，不碰别的会话
    pending = _pending_ids(paths, board) if hold_new_ready else set()
    changed = monitor_once(paths, board, close_panes=not dry_run)
    hold = {board.goal_id: _newly_ready(board, pending)} if hold_new_ready else None
    rr = retro_round(paths, dry_run)
    # 本 Job 刚收敛触发的复盘 Job 算本 Job 的延续：同一轮一起派出，免得驱动方还要再单独 tick 一次
    with jobs_locked(paths, rr["retro_spawned"]):
        retro_boards = [rb for g in rr["retro_spawned"] if (rb := Board.load(paths, g))]
        rounds = dispatch.run_dispatch_round(paths, [board, *retro_boards], dry_run, hold=hold)
    res = rounds[board.goal_id]
    monitor.flush_skips(paths, board)           # 收尾：把分派期失败引发的级联落盘 + 记事件
    return {"dispatched": res["dispatched"], "waiting_for_agent": res["waiting_for_agent"],
            "waiting_for_capacity": res["waiting_for_capacity"],
            "dispatch_errors": res["dispatch_errors"], "changed": changed,
            # hold_new_ready 留下的本轮新解锁节点：读完上游摘要（要改就 retask/skip）后**立即再 tick**派出
            "held": res["held"],
            # fan-out 模板 deps 已满足、等协调者读上游产出决定 K 后调 expand 展开——每轮醒目上报，
            # 否则协调者不知该展开、Job 会停在 AWAITING_EXPANSION 直到 idle 兜底。
            "awaiting_expansion": [t.task_id for t in board.expandable_tasks()],
            "complete": board.is_complete(), "summary": board.summary(),
            # 新建的复盘 Job（已随本轮派出）：驱动方收尾本 Job 后接着 monitor 它到收敛
            "retro_spawned": {g: rounds[g]["dispatched"] for g in rr["retro_spawned"] if g in rounds},
            "insights_new": rr["insights_new"]}


def tick_all(paths: Paths, dry_run: bool = False, hold_new_ready: bool = False) -> dict[str, Any]:
    """一轮调度**所有活跃 Job**：先各自监控收 outbox 释放容量，再跨 Job 汇总并发分派（含公平排序）。
    hold_new_ready 语义同 tick。"""
    # 锁住全部活跃 Job 再在锁内重新载入（按 goal_id 排序加锁，与其它多锁方不互锁）；后台 watch 每次只持一把锁。
    # 已收敛的 Job 不锁：watch 只碰有在途任务的 Job，它们不会被并发改。
    with jobs_locked(paths, [b.goal_id for b in Board.load_all(paths) if not b.is_complete()]):
        res = _tick_all_locked(paths, dry_run, hold_new_ready)
    if not dry_run and any(harvestable(paths, b) for b in Board.load_all(paths)):
        watch.ensure_running(paths)
    return res


def _tick_all_locked(paths: Paths, dry_run: bool, hold_new_ready: bool) -> dict[str, Any]:
    if not dry_run:
        reconcile_ledger(paths)
    boards = [b for b in Board.load_all(paths) if not b.is_complete()]
    changed_any = False
    hold: dict[str, set[str]] = {}
    for b in boards:                     # 先统一监控，收 outbox 推进各 Job、释放全局容量
        pending = _pending_ids(paths, b) if hold_new_ready else set()
        if monitor_once(paths, b, close_panes=not dry_run):
            changed_any = True
        if hold_new_ready:
            hold[b.goal_id] = _newly_ready(b, pending)
    rr = retro_round(paths, dry_run)     # 刚收敛的 Job → 复盘 Job，与其它 Job 同轮参与分派
    with jobs_locked(paths, rr["retro_spawned"]):
        boards += [rb for g in rr["retro_spawned"] if (rb := Board.load(paths, g))]
        rounds = dispatch.run_dispatch_round(paths, boards, dry_run, hold=hold)
    jobs: dict[str, Any] = {}
    dispatched_any = False
    for b in boards:
        r = rounds[b.goal_id]
        monitor.flush_skips(paths, b)           # 收尾：分派期失败引发的级联落盘 + 记事件
        if r["dispatched"]:
            dispatched_any = True
        jobs[b.goal_id] = {"dispatched": r["dispatched"],
                           "waiting_for_agent": r["waiting_for_agent"],
                           "waiting_for_capacity": r["waiting_for_capacity"],
                           "dispatch_errors": r["dispatch_errors"], "held": r["held"],
                           "awaiting_expansion": [t.task_id for t in b.expandable_tasks()],
                           "complete": b.is_complete(), "summary": b.summary()}
    all_boards = Board.load_all(paths)
    return {"jobs": jobs,
            "progressed": dispatched_any or changed_any or bool(rr["retro_spawned"]),
            "retro_spawned": rr["retro_spawned"], "insights_new": rr["insights_new"],
            "all_complete": all(b.is_complete() for b in all_boards) if all_boards else True}


def job_view(paths: Paths, b: Board) -> dict[str, Any]:
    """给协调者看的单 Job 判决位与待处置清单（monitor / wait 共用）。"""
    return {"complete": b.is_complete(), "has_failures": b.has_failures(),
            "stuck": is_stuck(b, paths),
            "awaiting": awaiting_tasks(paths, b),
            "awaiting_expansion": [t.task_id for t in b.expandable_tasks()],
            "retried": retrying_tasks(paths, b),
            "artifact_warnings": artifact_warnings(paths, b),
            "summary": b.summary()}


# wait 默认最长阻塞秒数：留在 shell 工具 120s 超时内，协调者始终在环内、不被动转后台。
WAIT_TIMEOUT = 90.0
# wait 每次收割的间隔：outbox 落盘后最多晚这么久被看到（逐轮 sleep+LLM 回合实测中位约 50s）。
WAIT_POLL = 3.0
# wait 输出里略去的回收审计类事件（量大、对决策无信息）。
_WAIT_QUIET_EVENTS = frozenset({"task_workspace_cleared", "pane_closed"})
_WAIT_MAX_EVENTS = 40


# 在途任务状态变化的事件：wait 以 run.log 为变化源，不管是自己、后台 watch 还是 tick 收割的都能看到。
_WAIT_CHANGE_EVENTS = frozenset({"task_done", "task_failed", "task_timeout", "task_fatal",
                                 "task_rejected", "reject_exhausted", "reject_invalid",
                                 "task_awaiting_input", "task_input_resumed"})


def _active_ids(paths: Paths, goal_id: str | None) -> list[str]:
    return [goal_id] if goal_id else Board.list_job_ids(paths)


def harvestable(paths: Paths, b: Board, rt: dict[str, Any] | None = None) -> list[str]:
    """真正还有东西可收割的在途任务：pollable 里去掉「打回触顶 parked」的——它的 pane 已回收、
    monitor 也跳过它，只等人裁决（可能几天）。把它算在途，watch 就永远不会 idle 退出、wait 白等满超时。"""
    rt = ledger.load(paths) if rt is None else rt
    return [t.task_id for t in b.pollable_tasks()
            if not (t.status == AWAITING_INPUT
                    and rt.get(runtime_key(b.goal_id, t.task_id), {}).get("escalated"))]


def harvest_round(paths: Paths, goal_id: str | None = None) -> dict[str, Any]:
    """收割一遍（只 monitor、不分派）：逐个 Job 在 job_lock 内**重新载入**再 monitor_once，
    不拿陈旧内存态覆盖并发驱动者（协调者 CLI / 后台 watch / run）的改动。wait 与 watch 共用。
    返回 {"inflight": 是否还有在途任务, "changed": {goal_id: [task_id…]}}。"""
    inflight = False
    changed: dict[str, list[str]] = {}
    for gid in _active_ids(paths, goal_id):
        peek = Board.load(paths, gid)          # 锁外先瞄一眼：没有在途的 Job（绝大多数历史 Job）不必抢锁
        if peek is None or not harvestable(paths, peek):
            continue
        with job_lock(paths, gid):
            b = Board.load(paths, gid)
            if b is None or not harvestable(paths, b):
                continue
            if c := monitor_once(paths, b, close_panes=True):
                changed[gid] = c
            inflight = inflight or bool(harvestable(paths, b))
    return {"inflight": inflight, "changed": changed}


def wait_for_change(paths: Paths, goal_id: str | None = None, timeout: float = WAIT_TIMEOUT,
                    poll: float = WAIT_POLL, since: int | None = None) -> dict[str, Any]:
    """阻塞到在途任务有状态变化（交付/失败/打回/进出等人）或超时；**只收割、不分派**。

    逐轮驱动里替代「sleep N; monitor」：每 poll 秒收割一次，一有变化立即返回，协调者读 `changed`
    里刚变化节点的 result_summary 决定 skip/retask/expand/answer，再 tick 派出 `ready` 里的下游
    ——它们此刻尚未派发，改 goal 不必 --force。无在途任务时立即返回（该 tick / 已收敛 / 卡死）。

    变化以 run.log 事件为准（游标 since 之后的 _WAIT_CHANGE_EVENTS），所以后台 `watch` 或上一轮
    tick 已经收割掉的交付也会被报出，不会因为「我这次 monitor 没收到」而漏看。
    since：上次返回的 cursor；缺省从当前末尾起。游标每轮不同，也让逐轮命令自带区分度，不撞 Orca
    的重复命令护栏。"""
    cursor = ledger.event_cursor(paths) if since is None else since
    t0 = time.monotonic()
    deadline = t0 + max(0.0, timeout)
    events: list[dict[str, Any]] = []
    while True:
        inflight = harvest_round(paths, goal_id)["inflight"]
        evs, cursor = ledger.read_events(paths, cursor)
        events += [e for e in evs if e.get("goal_id") and e.get("event") not in _WAIT_QUIET_EVENTS
                   and (goal_id is None or e["goal_id"] == goal_id)]
        if any(e["event"] in _WAIT_CHANGE_EVENTS for e in events):
            reason = "changed"
            break
        if not inflight:
            reason = "no_inflight"
            break
        left = deadline - time.monotonic()
        if left <= 0:
            reason = "timeout"
            break
        time.sleep(min(poll, left))
    rr = retro_round(paths)
    changed: dict[str, list[str]] = {}
    for e in events:
        if e["event"] in _WAIT_CHANGE_EVENTS:
            ids = changed.setdefault(e["goal_id"], [])
            for tid in (e.get("task_id"), e.get("from_task")):
                if tid and tid not in ids:
                    ids.append(tid)
    jobs: dict[str, Any] = {}
    for gid in _active_ids(paths, goal_id):
        b = Board.load(paths, gid)
        if b is None or (b.is_complete() and gid not in changed):
            continue              # 早已收敛的旧 Job 不列；本次刚收敛的要给出（判决位就在它身上）
        view = job_view(paths, b)
        view["changed"] = [{"task_id": tid, "status": t.status, "summary": t.result_summary,
                            "artifacts": t.artifacts, "error": t.error}
                           for tid in changed.get(gid, []) if (t := b.tasks.get(tid))]
        # 下次 tick 会派出的节点：要 retask/skip 就趁现在（未派发，不必 --force）
        view["ready"] = [t.task_id for t in b.ready_tasks()]
        jobs[gid] = view
    out: dict[str, Any] = {"reason": reason, "waited_s": round(time.monotonic() - t0, 1),
                           "cursor": cursor, "events": events[-_WAIT_MAX_EVENTS:], "jobs": jobs}
    if rr["retro_spawned"] or rr["insights_new"]:
        out["retro"] = rr
    return out


def is_stuck(board: Board, paths: Paths | None = None) -> bool:
    """卡死判定：已无法推进。

    - 无 ready、无在途(dispatched/awaiting_input) → 卡死（旧判据）。
    - 还有 dispatched → 未卡死（monitor 会靠超时/存活兜底推进）。
    - 还有 awaiting_input → 未卡死：worker 合法地停下等人拍板，靠 answer/人工推进（非死锁）。
    - 还有 awaiting_expansion → 未卡死：fan-out 模板等协调者读上游产出后调 expand 展开（靠人/协调者推进，非死锁）。
    - 有 ready 但全部「无能力匹配的 agent」→ 永久卡死（区别于容量暂满可等）。
    """
    if board.is_complete():
        return False
    if board.pollable_tasks():          # dispatched + awaiting_input：都算「在途」，非卡死
        return False
    if board.expandable_tasks():        # awaiting_expansion：等协调者展开，非死锁
        return False
    ready = board.ready_tasks()         # 触发 refresh_ready：把失败上游的 pending 下游级联成 skipped
    if board.is_complete():             # 级联后可能已全终态——刚收敛的 board 不是卡死
        return False
    if not ready:
        return True
    if paths is not None:
        agents = registry.list_agents(paths)
        if all(not dispatch.routable(paths, t, agents) for t in ready):
            return True
    return False


def _final_status(paths: Paths, base: str) -> str:
    """全部收敛时区分 complete / completed_with_failures，让自动驾驶方一眼看出结果。"""
    if base == "complete" and any(b.has_failures() for b in Board.load_all(paths)):
        return "completed_with_failures"
    return base


def _snapshot(paths: Paths) -> dict[str, Any]:
    """只读全量快照（含已完成 Job），用于 run 收尾汇总——绝不产生副作用。

    旧实现在 max_ticks 分支用 `tick_all(dry_run=True)` 出汇总，而 dry-run 并非只读：
    它会写 inbox、落 <dry-run> runtime 幽灵条目、把 ready 翻成 dispatched，污染真实状态
    并让后续真实 run 要等 900s 超时才自愈。这里改为纯只读，且覆盖全部 Job（不漏先完成者）。
    """
    boards = Board.load_all(paths)
    return {"jobs": {b.goal_id: {"complete": b.is_complete(),
                                 "has_failures": b.has_failures(),
                                 "summary": b.summary()} for b in boards},
            "all_complete": all(b.is_complete() for b in boards) if boards else True}


def run_loop(paths: Paths, interval: float = 3.0, max_ticks: int = 200,
             dry_run: bool = False, goal_id: str | None = None) -> dict[str, Any]:
    """持续调度直到全部完成 / 卡死 / 到达 max_ticks。goal_id 给定则只跑该 Job（与 tick/monitor 一致）。

    goal_id 模式下，该 Job 收敛触发的复盘 Job 也一并跟着驱动到收敛（follow），不留给没人收割的 worker。"""
    ticks = 0
    idle_streak = 0                      # 连续「零分派+零变化+零在途」轮数：容量停滞/挂死兜底
    follow = [goal_id] if goal_id else []
    while ticks < max_ticks:
        if goal_id:
            b = Board.load(paths, goal_id)
            if b is None:
                return {"status": "error", "error": f"未找到 Job: {goal_id}", "ticks": ticks}
            if not dry_run:
                # 只跑单 Job 时也轻量监控**兄弟 Job**：否则兄弟里「已写完 outbox 但没人收割」的
                # dispatched 条目会一直算作 live、永久占用全局 agent 容量，把本 Job 拖成假 stuck。
                for sib in Board.list_job_ids(paths):
                    if sib not in follow:
                        harvest_round(paths, sib)
            res = tick(paths, b, dry_run=dry_run)
            progressed = bool(res["dispatched"] or res["changed"] or res["retro_spawned"])
            follow += [g for g in res["retro_spawned"] if g not in follow]
            for g in follow[1:]:
                fb = Board.load(paths, g)
                if fb is not None and not fb.is_complete():
                    fr = tick(paths, fb, dry_run=dry_run)
                    progressed = progressed or bool(fr["dispatched"] or fr["changed"])
            all_complete = all(fb is None or fb.is_complete()
                               for fb in (Board.load(paths, g) for g in follow))
        else:
            res = tick_all(paths, dry_run=dry_run)
            all_complete = res["all_complete"]
            progressed = res.get("progressed", False)
        ticks += 1
        if all_complete:
            return {"status": _final_status(paths, "complete"), "ticks": ticks,
                    **_snapshot(paths)}
        active = [b for b in Board.load_all(paths) if not b.is_complete()]
        if goal_id:
            active = [b for b in active if b.goal_id in follow]
        if active and all(is_stuck(b, paths) for b in active):
            return {"status": "stuck", "ticks": ticks, **_snapshot(paths)}
        inflight = any(b.dispatched_tasks() for b in active)
        # fan-out 待展开短路：本轮没进展、没在途、却有 fan-out 模板等展开 → 立刻返回 awaiting_expansion，
        # 不必空转满 IDLE_STREAK_LIMIT 轮（自动 run 无法替 LLM 决定 K，早点把「该展开了」交回驱动方）。
        if not progressed and not inflight:
            expandable = sorted(t.task_id for b in active for t in b.expandable_tasks())
            if expandable:
                return {"status": "awaiting_expansion", "awaiting_expansion": expandable,
                        "reason": "fan-out 模板待协调者展开(expand)", "ticks": ticks,
                        **_snapshot(paths)}
        # 容量停滞兜底：无分派、无变化、且全局无在途任务（workers 都不在跑），却仍有活跃 Job
        # → 卡住了（如反向孤儿占死稀缺容量、或所有 ready 都被容量挡住而容量永不释放）。
        idle_streak = 0 if (progressed or inflight) else idle_streak + 1
        if idle_streak >= IDLE_STREAK_LIMIT:
            log.warning("run_loop 判定容量停滞：连续 %d 轮无分派/无变化/无在途任务", idle_streak)
            return {"status": "stuck", "reason": "no progress (capacity stall / wedged)",
                    "ticks": ticks, **_snapshot(paths)}
        if dry_run:                      # dry-run 无真实 outbox，跑一轮即停
            return {"status": "dry-run", "ticks": ticks, **res}
        time.sleep(interval)
    return {"status": "max_ticks", "ticks": ticks, **_snapshot(paths)}
