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
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import dispatch, monitor, registry
from .board import Board
from .config import Paths
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


def tick(paths: Paths, board: Board, dry_run: bool = False) -> dict[str, Any]:
    """单 Job 一轮调度：**先监控收 outbox 释放容量、再（并发）分派** ready 节点。

    monitor-先-dispatch：本轮完成的任务释放的容量，在**同一轮**就被新 ready 节点填上。
    分派末尾统一 refresh_ready + 落盘 + 记 skip 事件，让「分派期失败(如 provisioning 耗尽)」
    触发的级联 SKIPPED 也能持久化并可观测（否则 run 报完成、磁盘却停在 pending 发散）。
    """
    if not dry_run:
        reconcile_ledger(paths, scope_goal_id=board.goal_id)   # 只对账自己这个 Job，不碰别的会话
    changed = monitor_once(paths, board, close_panes=not dry_run)
    res = dispatch.run_dispatch_round(paths, [board], dry_run)[board.goal_id]
    monitor.flush_skips(paths, board)           # 收尾：把分派期失败引发的级联落盘 + 记事件
    return {"dispatched": res["dispatched"], "waiting_for_agent": res["waiting_for_agent"],
            "waiting_for_capacity": res["waiting_for_capacity"],
            "dispatch_errors": res["dispatch_errors"], "changed": changed,
            # fan-out 模板 deps 已满足、等协调者读上游产出决定 K 后调 expand 展开——每轮醒目上报，
            # 否则协调者不知该展开、Job 会停在 AWAITING_EXPANSION 直到 idle 兜底。
            "awaiting_expansion": [t.task_id for t in board.expandable_tasks()],
            "complete": board.is_complete(), "summary": board.summary()}


def tick_all(paths: Paths, dry_run: bool = False) -> dict[str, Any]:
    """一轮调度**所有活跃 Job**：先各自监控收 outbox 释放容量，再跨 Job 汇总并发分派（含公平排序）。"""
    if not dry_run:
        reconcile_ledger(paths)
    boards = [b for b in Board.load_all(paths) if not b.is_complete()]
    changed_any = False
    for b in boards:                     # 先统一监控，收 outbox 推进各 Job、释放全局容量
        if monitor_once(paths, b, close_panes=not dry_run):
            changed_any = True
    rounds = dispatch.run_dispatch_round(paths, boards, dry_run)
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
                           "dispatch_errors": r["dispatch_errors"],
                           "awaiting_expansion": [t.task_id for t in b.expandable_tasks()],
                           "complete": b.is_complete(), "summary": b.summary()}
    all_boards = Board.load_all(paths)
    return {"jobs": jobs,
            "progressed": dispatched_any or changed_any,
            "all_complete": all(b.is_complete() for b in all_boards) if all_boards else True}


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
    """持续调度直到全部完成 / 卡死 / 到达 max_ticks。goal_id 给定则只跑该 Job（与 tick/monitor 一致）。"""
    ticks = 0
    idle_streak = 0                      # 连续「零分派+零变化+零在途」轮数：容量停滞/挂死兜底
    while ticks < max_ticks:
        if goal_id:
            b = Board.load(paths, goal_id)
            if b is None:
                return {"status": "error", "error": f"未找到 Job: {goal_id}", "ticks": ticks}
            if not dry_run:
                # 只跑单 Job 时也轻量监控**兄弟 Job**：否则兄弟里「已写完 outbox 但没人收割」的
                # dispatched 条目会一直算作 live、永久占用全局 agent 容量，把本 Job 拖成假 stuck。
                for sib in Board.load_all(paths):
                    if sib.goal_id != goal_id and not sib.is_complete():
                        monitor_once(paths, sib, close_panes=True)
            res = tick(paths, b, dry_run=dry_run)
            all_complete = res["complete"]
            progressed = bool(res["dispatched"] or res["changed"])
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
            active = [b for b in active if b.goal_id == goal_id]
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
