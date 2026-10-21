"""执行计时：Task 运行记录（runs）与 coordinator 各状态迁移的埋点。

不碰真实 herdr：派发走 dry-run，存活/等人判定 monkeypatch 掉，时间用可控假时钟推进。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from agent_team import coordinator, dispatch, herdr, ledger, pane_scan, reclaim
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import (Task, AgentManifest, DISPATCHED, DONE, PENDING,
                               AWAITING_INPUT, BYPASSED, READY)


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def paths(tmp_path, monkeypatch):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    p = Paths(tmp_path)
    # monitor 收尾会回收 pane / 查 eligible agent：与计时无关，全部打桩
    monkeypatch.setattr(reclaim, "close_pane_safe", lambda *a, **k: None)
    monkeypatch.setattr(reclaim, "clear_task_workspace", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "all_eligible_tried", lambda *a, **k: True)
    return p


def _board(paths: Paths, *tasks: Task) -> Board:
    b = Board("g1", "goal", list(tasks))
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def _dispatch(paths, board, tid, agent="a1"):
    coordinator.dispatch_task(paths, board, board.tasks[tid],
                              manifest=AgentManifest(name=agent), dry_run=True)


def _outbox(paths, agent, tid, status="done", mtime=None, **extra):
    d = paths.outbox(agent)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"g1__{tid}.json"
    f.write_text(json.dumps({"task_id": tid, "status": status, "summary": "s", **extra}),
                 encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# ---------- 模型层 ----------

def test_run_lifecycle_and_awaiting_excluded():
    t = Task("t", "g")
    t.ready_at = 90.0
    t.attempts = 1
    t.open_run("a1", 100.0, provision_s=4.0)
    t.mark_awaiting(130.0)
    t.mark_awaiting(140.0)                 # 已在等：锚点不动
    t.mark_resumed(190.0)
    run = t.close_run("done", 250.0)
    assert run["queue_s"] == 10.0 and run["provision_s"] == 4.0
    assert run["wall_s"] == 150.0 and run["awaiting_s"] == 60.0 and run["active_s"] == 90.0
    assert run["end_source"] == "observed" and t.current_run() is None
    assert t.close_run("done", 300.0) is None          # 无在开记录：no-op


def test_close_run_uses_outbox_mtime_clamped():
    t = Task("t", "g")
    t.open_run("a1", 100.0)
    run = t.close_run("done", 500.0, ended_at=160.0)
    assert run["ended_at"] == 160.0 and run["end_source"] == "outbox_mtime"
    t.open_run("a1", 600.0)
    assert t.close_run("done", 700.0, ended_at=10.0)["ended_at"] == 600.0   # 早于开始：夹到 started_at


def test_close_run_while_awaiting_counts_wait():
    t = Task("t", "g")
    t.open_run("a1", 0.0)
    t.mark_awaiting(10.0)
    run = t.close_run("bypassed", 70.0)
    assert run["awaiting_s"] == 60.0 and run["active_s"] == 10.0


def test_timing_live_view_and_legacy_task():
    t = Task.from_dict({"task_id": "old", "goal": "g", "status": "done"})   # 旧 board：无 runs
    assert t.timing(0.0)["runs"] == 0 and t.timing(0.0)["active_s"] is None
    t.open_run("a1", 100.0)
    tm = t.timing(130.0)
    assert tm["running"] and tm["active_s"] == 30.0 and t.runs[0]["wall_s"] is None


# ---------- coordinator 埋点 ----------

def test_dispatch_done_records_queue_and_outbox_time(paths, clock):
    b = _board(paths, Task("t1", "g"))
    clock.tick(5)
    _dispatch(paths, b, "t1")
    run = b.tasks["t1"].runs[0]
    assert run["queue_s"] == 5.0 and run["started_at"] == clock.t
    start = clock.t
    _outbox(paths, "a1", "t1", mtime=start + 40)
    clock.tick(300)                         # monitor 晚 300s 才观测到
    coordinator.monitor_once(paths, b, close_panes=False)
    t = Board.load(paths, "g1").tasks["t1"]
    assert t.status == DONE
    assert t.runs[0]["outcome"] == "done" and t.runs[0]["active_s"] == 40.0
    assert t.runs[0]["end_source"] == "outbox_mtime"


def test_failed_retry_keeps_both_runs(paths, clock):
    b = _board(paths, Task("t1", "g", max_retries=1))
    _dispatch(paths, b, "t1")
    b.tasks["t1"].attempts = 1                # dry-run 不 +attempts；模拟真实接单
    _outbox(paths, "a1", "t1", status="failed", mtime=clock.t + 20)
    clock.tick(30)
    coordinator.monitor_once(paths, b, close_panes=False)
    assert b.tasks["t1"].status == READY             # 未超重试上限：回待派，monitor 收尾已 refresh_ready
    _dispatch(paths, b, "t1", agent="a2")
    _outbox(paths, "a2", "t1", mtime=clock.t + 50)
    clock.tick(60)
    coordinator.monitor_once(paths, b, close_panes=False)
    t = b.tasks["t1"]
    assert [r["outcome"] for r in t.runs] == ["failed", "done"]
    assert [r["agent"] for r in t.runs] == ["a1", "a2"]
    assert t.timing(clock.t)["total_active_s"] == 70.0
    agg = {a["agent"]: a for a in coordinator.timing_by_agent(b, clock.t)}
    assert agg["a1"]["failed"] == 1 and agg["a2"]["done"] == 1 and agg["a2"]["avg_active_s"] == 50.0


def _make_live(paths, b, tid, monkeypatch, state):
    """把 dry-run 派发的账本条目改成「真 pane」，并让 herdr 报指定状态。"""
    rt = ledger.load(paths)
    rt[f"g1:{tid}"].update(pane_id="p1", herdr_name="h1", last_alive_at=time.time())
    ledger.save(paths, rt)
    monkeypatch.setattr(herdr, "agent_status", lambda n: state["st"])
    monkeypatch.setattr(pane_scan, "read_tail", lambda *a, **k: "")
    monkeypatch.setattr(pane_scan, "fatal_reason", lambda *a, **k: None)
    monkeypatch.setattr(pane_scan, "awaiting_reason", lambda *a, **k: state.get("ask"))


def test_awaiting_then_resume_excluded_from_active(paths, clock, monkeypatch):
    b = _board(paths, Task("t1", "g"))
    _dispatch(paths, b, "t1")
    state = {"st": "working"}
    _make_live(paths, b, "t1", monkeypatch, state)
    clock.tick(20)
    coordinator.monitor_once(paths, b, close_panes=False)          # working：仍在跑
    state.update(st="idle", ask="要用哪个分支？")
    coordinator.monitor_once(paths, b, close_panes=False)          # t=20 进入等人
    assert b.tasks["t1"].status == AWAITING_INPUT
    clock.tick(100)
    state.update(st="working", ask=None)
    coordinator.monitor_once(paths, b, close_panes=False)          # t=120 恢复
    assert b.tasks["t1"].status == DISPATCHED
    clock.tick(30)
    _outbox(paths, "a1", "t1", mtime=clock.t)
    coordinator.monitor_once(paths, b, close_panes=False)
    run = b.tasks["t1"].runs[0]
    assert run["wall_s"] == 150.0 and run["awaiting_s"] == 100.0 and run["active_s"] == 50.0


def test_reject_closes_rejecter_and_resets_target(paths, clock):
    b = _board(paths, Task("impl", "g"), Task("rev", "g", needs=["impl"], reject_to=["impl"]))
    _dispatch(paths, b, "impl")
    _outbox(paths, "a1", "impl", mtime=clock.t + 10)
    clock.tick(10)
    coordinator.monitor_once(paths, b, close_panes=False)
    _dispatch(paths, b, "rev", agent="r1")
    _outbox(paths, "r1", "rev", status="reject", reject_to="impl", mtime=clock.t + 25)
    clock.tick(30)
    coordinator.monitor_once(paths, b, close_panes=False)
    assert b.tasks["impl"].status == READY and b.tasks["rev"].status == PENDING
    assert b.tasks["rev"].runs[0]["outcome"] == "reject"
    assert b.tasks["rev"].runs[0]["active_s"] == 25.0
    assert b.tasks["impl"].runs[0]["outcome"] == "done"            # 历史保留，不被重置清掉


def test_bypass_and_retask_close_inflight_runs(paths, clock):
    b = _board(paths, Task("a", "g"), Task("b", "g"))
    _dispatch(paths, b, "a")
    _dispatch(paths, b, "b")
    clock.tick(15)
    coordinator.bypass_tasks(paths, b, ["a"], reason="不需要")
    assert b.tasks["a"].status == BYPASSED and b.tasks["a"].runs[0]["outcome"] == "bypassed"
    coordinator.retask_task(paths, b, "b", append="补充说明", force=True)
    assert b.tasks["b"].runs[0]["outcome"] == "retasked"
    assert b.tasks["b"].runs[0]["active_s"] == 15.0 and b.tasks["b"].current_run() is None


def test_board_completion_stamp_and_report(paths, clock):
    b = _board(paths, Task("t1", "g"))
    created = b.created_at
    _dispatch(paths, b, "t1")
    _outbox(paths, "a1", "t1", mtime=clock.t + 30)
    clock.tick(45)
    coordinator.monitor_once(paths, b, close_panes=False)
    loaded = Board.load(paths, "g1")
    assert loaded.created_at == created and loaded.completed_at == clock.t
    rep = coordinator.report(paths, "g1")
    assert rep["timing"]["wall_s"] == 45.0 and rep["timing"]["total_active_s"] == 30.0
    assert rep["tasks"][0]["timing"]["outcome"] == "done"
    assert rep["agent_timing"][0]["agent"] == "a1"


def test_legacy_board_without_timing_fields(paths):
    paths.jobs_dir.mkdir(parents=True)
    paths.job_file("old").write_text(json.dumps({
        "goal_id": "old", "goal": "g", "version": 1,
        "tasks": [{"task_id": "x", "goal": "g", "status": "done", "agent": "a1"}]}),
        encoding="utf-8")
    rep = coordinator.report(paths, "old")
    assert rep["timing"]["wall_s"] is None and rep["agent_timing"] == []
    assert rep["tasks"][0]["timing"]["runs"] == 0
