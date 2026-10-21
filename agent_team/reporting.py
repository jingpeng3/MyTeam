"""只读汇总：report、计时聚合、以及 monitor/status 输出里的等人/重试/产物告警视图。
"""
from __future__ import annotations

import time
from typing import Any

from . import ledger
from .board import Board
from .config import Paths
from .models import (Task, FAILED, SKIPPED, READY, PENDING,
                     BYPASSED, AWAITING_INPUT, AWAITING_EXPANSION, runtime_key)


def retrying_tasks(paths: Paths, board: Board) -> list[dict[str, Any]]:
    """已执行过、在重试预算内被判失败、当前退回待派等待重新分派的任务（「静默重试」）。

    判据：status ∈ {PENDING, READY} 且 attempts>=1——即它真正接单跑过（attempts 只在 worker 接单才 +1）、
    这一轮 monitor 把它判失败退回 PENDING（未超 max_retries），正等下一次 tick 重派。用于让协调者
    在每轮汇报里看见「交付缺失/超时→退回重试」这类过去被淹没在 `changed` 里、又因未终态而不进
    `has_failures` 的静默重试。区别于：全新未派任务(attempts=0)、在途任务(DISPATCHED)、
    打回重置任务(reject 把 attempts 归零→attempts=0 不计)、重试耗尽(FAILED)。"""
    out: list[dict[str, Any]] = []
    for task in board.tasks.values():
        if task.status not in (PENDING, READY) or task.attempts < 1:
            continue
        out.append({"task_id": task.task_id,
                    "attempts": task.attempts,          # 已尝试次数
                    "max_retries": task.max_retries,
                    "next_attempt": task.attempts + 1,  # 下次 tick 将是第几次尝试
                    "last_error": task.error or ""})    # 上次失败原因（如 delivery missing/超时）
    return out


def artifact_warnings(paths: Paths, board: Board) -> list[dict[str, Any]]:
    """已交付但「报了的产物落盘查无此文件」的任务，供协调者每轮看见并处置。

    这类交付**算完成**（不失败），但下游很可能读到空/缺文件——提前 surface 比等下游崩了再回溯便宜。
    """
    return [{"task_id": t.task_id, "missing": list(t.artifact_warnings)}
            for t in board.tasks.values() if t.artifact_warnings]


def awaiting_tasks(paths: Paths, board: Board) -> list[dict[str, Any]]:
    """当前 Job 内处于 AWAITING_INPUT（停下等人拍板）的任务及其问题摘要，供 CLI 上报给协调者。"""
    rt = ledger.load(paths)
    out: list[dict[str, Any]] = []
    for task in board.tasks.values():
        if task.status != AWAITING_INPUT:
            continue
        info = rt.get(runtime_key(board.goal_id, task.task_id), {})
        out.append({"task_id": task.task_id, "agent": task.agent,
                    "question": info.get("awaiting_reason", ""),
                    # escalated=True 表示「打回触顶待裁决」（该走 reject --force/收尾），
                    # False 表示普通等人（该走 answer 注入）——让消费方程序化区分两类处置路径。
                    "escalated": bool(info.get("escalated"))})
    return out


_FAIL_OUTCOMES = frozenset({"failed", "timeout", "fatal"})


def timing_by_agent(board: Board, now: float) -> list[dict[str, Any]]:
    """按 agent 聚合本 Job 全部运行记录：次数、成败、净执行总/均/最长、等人累计、在跑数。

    均值/最长只统计**已收尾**的运行（在跑的还没结束，混进来会把均值拉低）；总计含在跑的当前值。
    返回按 total_active_s 降序，最耗时的 agent 排前。"""
    agg: dict[str, dict[str, Any]] = {}
    for t in board.tasks.values():
        for r in t.runs:
            live = Task.run_live(r, now)
            a = agg.setdefault(r.get("agent") or "?", {
                "agent": r.get("agent") or "?", "runs": 0, "done": 0, "failed": 0,
                "running": 0, "total_active_s": 0.0, "total_awaiting_s": 0.0,
                "_closed": []})
            a["runs"] += 1
            a["total_active_s"] += live.get("active_s") or 0.0
            a["total_awaiting_s"] += live.get("awaiting_s") or 0.0
            if r.get("ended_at") is None:
                a["running"] += 1
                continue
            a["_closed"].append(live.get("active_s") or 0.0)
            if r.get("outcome") == "done":
                a["done"] += 1
            elif r.get("outcome") in _FAIL_OUTCOMES:
                a["failed"] += 1
    out = []
    for a in agg.values():
        closed = a.pop("_closed")
        a["total_active_s"] = round(a["total_active_s"], 3)
        a["total_awaiting_s"] = round(a["total_awaiting_s"], 3)
        a["avg_active_s"] = round(sum(closed) / len(closed), 3) if closed else None
        a["max_active_s"] = round(max(closed), 3) if closed else None
        out.append(a)
    out.sort(key=lambda a: -a["total_active_s"])
    return out


def job_timing(board: Board, now: float) -> dict[str, Any]:
    """Job 级计时：墙钟（plan 载入→收敛/当前）+ 各任务净执行之和（并行时可大于墙钟）。"""
    return {"created_at": board.created_at, "completed_at": board.completed_at,
            "wall_s": board.wall_s(now),
            "total_active_s": round(sum(t.timing(now)["total_active_s"]
                                        for t in board.tasks.values()), 3)}


def _report_board(board: Board) -> dict[str, Any]:
    now = time.time()
    tasks = [{"task_id": t.task_id, "status": t.status, "agent": t.agent,
              "attempts": t.attempts, "reject_count": t.reject_count,
              "summary": t.result_summary,
              "artifacts": t.artifacts, "error": t.error,
              "timing": t.timing(now)}
             for t in board.tasks.values()]
    failed = [t["task_id"] for t in tasks if t["status"] == FAILED]
    skipped = [t["task_id"] for t in tasks if t["status"] == SKIPPED]
    bypassed = [t["task_id"] for t in tasks if t["status"] == BYPASSED]
    # 停在等人拍板（含打回升级）的节点：让 report 一眼区分「等人」与「在跑」——否则二者顶层
    # complete=False/ok=False 完全一样，消费方无从知道 Job 其实卡在人工介入。与 status 的 awaiting 对齐。
    awaiting = [t["task_id"] for t in tasks if t["status"] == AWAITING_INPUT]
    # fan-out 模板 deps 已满足、等协调者调 expand 展开的节点：让 report 一眼看出 Job 卡在待展开。
    awaiting_expansion = [t["task_id"] for t in tasks if t["status"] == AWAITING_EXPANSION]
    # 被打回过的步骤（含次数）：供复盘看哪一步返工过、返工几次——即便最终收敛也留痕。
    rejected = [{"task_id": t.task_id, "count": t.reject_count}
                for t in board.tasks.values() if t.reject_count]
    return {"goal_id": board.goal_id, "goal": board.goal,
            "complete": board.is_complete(),
            "ok": board.is_complete() and not board.has_failures(),
            "failed": failed, "skipped": skipped, "bypassed": bypassed,
            "awaiting": awaiting, "awaiting_expansion": awaiting_expansion,
            "rejected": rejected,
            "artifacts_dir": f"shared/artifacts/{board.goal_id}",
            "timing": job_timing(board, now),
            "agent_timing": timing_by_agent(board, now),
            "summary": board.summary(), "tasks": tasks}


def report(paths: Paths, goal_id: str | None = None) -> dict[str, Any]:
    """汇总结果：给定 goal_id 出单 Job，否则聚合所有 Job。"""
    if goal_id:
        b = Board.load(paths, goal_id)
        if b is None:
            return {"error": f"未找到 Job: {goal_id}"}
        return _report_board(b)
    boards = Board.load_all(paths)
    return {"jobs": [_report_board(b) for b in boards],
            "all_complete": all(b.is_complete() for b in boards) if boards else True}
