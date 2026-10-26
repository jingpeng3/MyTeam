"""runtime 账本（coordinator/state/runtime.json）的加锁读写与合并提交，以及调度事件日志 run.log。

账本键为 `<goal_id>:<task_id>`（models.runtime_key），值记录在途 pane/herdr 名/session/存活时钟等。
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from .config import Paths, atomic_write_json, read_json_or, file_lock


def _runtime_file(paths: Paths) -> Path:
    return paths.runtime_file


def load(paths: Paths) -> dict[str, Any]:
    return read_json_or(_runtime_file(paths), {})   # 容错：损坏不拖垮整轮


def save(paths: Paths, data: dict[str, Any]) -> None:
    atomic_write_json(_runtime_file(paths), data)   # 原子落盘，崩溃不留半截账本


@contextlib.contextmanager
def lock(paths: Paths):
    """跨进程互斥 runtime.json 的「读-改-写」。runtime.json 是回收 pane 的唯一账本，却曾是
    唯一无锁的读改写（board.save/team.yaml 都有 file_lock）——并发驱动者(后台 run + 手动命令、
    或多协调者进程共享 state)会整字典互相覆盖丢更新，导致关错 pane/孤儿 pane/ledger-lost 假失败。
    离散写点(分派提交/回收/绕过/改派/打回/答复)用它把 load..save 整段纳入临界区，原子且不复活
    并发删除。临界区内只做本进程的读改写(必要的 herdr 回收在锁内但都有超时)，不长时间持锁。"""
    with file_lock(_runtime_file(paths)):
        yield


def commit_merge(paths: Paths, rt: dict[str, Any],
                          original_keys: set[str]) -> None:
    """给「持有全量 rt、只更新/删除已有条目、从不新增条目」的长流程(monitor_once/reconcile_ledger)
    用的收尾合并：不在 herdr 轮询期间长持锁，只在最后一步锁内把本进程的改动合并进磁盘最新态——
    本进程删除的 key 生效删除、持有/更新的 key 覆盖写，**并发新增的 key(本进程从未见过)因保留磁盘态
    而存活**（这正是丢更新里最常见、危害最大的「新分派条目被覆盖」场景）。"""
    removed = set(original_keys) - set(rt)
    with file_lock(_runtime_file(paths)):
        disk = read_json_or(_runtime_file(paths), {})
        for k in removed:
            disk.pop(k, None)
        disk.update(rt)
        atomic_write_json(_runtime_file(paths), disk)


def log_event(paths: Paths, event: str, **fields: Any) -> None:
    """把一条调度事件追加进 coordinator/state/run.log（JSONL），供事后复盘。

    旧实现除最终汇总 dict 外零可观测性，降级/卡死/泄漏全靠 traceback 猜。
    """
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event, **fields}
        with open(paths.state_dir / "run.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志是旁路，绝不因写日志失败影响调度


def event_cursor(paths: Paths) -> int:
    """run.log 当前末尾的字节偏移，作 read_events 的游标。"""
    try:
        return (paths.state_dir / "run.log").stat().st_size
    except OSError:
        return 0


def read_events(paths: Paths, since: int) -> tuple[list[dict[str, Any]], int]:
    """读 run.log 中游标 since 之后的完整事件行，返回 (事件列表, 新游标)。
    游标越界（日志被换掉/截短）按当前末尾处理；末尾未写完的半行留给下次读。"""
    f = paths.state_dir / "run.log"
    try:
        with open(f, "rb") as fh:
            size = fh.seek(0, 2)
            since = min(max(0, since), size)
            fh.seek(since)
            data = fh.read()
    except OSError:
        return [], since
    end = data.rfind(b"\n") + 1
    out: list[dict[str, Any]] = []
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, since + end


def active_by_job_agent(paths: Paths) -> dict[tuple[str, str], int]:
    """当前每个 (Job, agent) 占用的 pane(dispatched 任务)数——**按 Job 独立计容量**。

    max_concurrent 是「单个 Job 内某 agent 的并发上限」，不跨 Job 共享：两个 Job 各自最多
    跑满 max_concurrent 个同一 agent 的 pane，互不挤占。键用 (goal_id, agent)。
    """
    counts: dict[tuple[str, str], int] = {}
    for info in load(paths).values():
        a = info.get("agent")
        g = info.get("goal_id")
        if a and g:
            counts[(g, a)] = counts.get((g, a), 0) + 1
    return counts
