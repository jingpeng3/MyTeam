"""coordinator 端到端行为：真实分派路径（herdr 打桩）、monitor 各判定分支、三杠杆与回收。

herdr 全部替换为内存假实现，其余（runtime 账本、信号文件、workspace 目录、board 落盘）都走真实代码，
用来给 coordinator 的重构兜底：回收在途任务时 pane/账本/信号/workspace 必须一起收干净。
"""
from __future__ import annotations

import json
import time

import pytest
import yaml

from agent_team import coordinator, herdr
from agent_team.board import Board
from agent_team.config import Paths, resolve_task_workspace, load_manifest
from agent_team.models import (Task, DISPATCHED, DONE, FAILED, READY, PENDING, BYPASSED,
                               AWAITING_INPUT)


class FakeHerdr:
    def __init__(self):
        self.n = 0
        self.status: dict[str, str] = {}
        self.text: dict[str, str] = {}
        self.closed: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def create_pane(self, *a, **k):
        self.n += 1
        return f"p{self.n}"

    def start_agent(self, pane_id, *a, **k):
        return f"h{pane_id}"

    def prompt_agent(self, target, text, *a, **k):
        self.prompts.append((target, text))

    def agent_status(self, target):
        return self.status.get(target, "working")

    def read_agent(self, target, lines=120, session=None):
        return self.text.get(target, "")

    def close_pane(self, pane_id):
        self.closed.append(pane_id)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


@pytest.fixture
def fake(monkeypatch):
    f = FakeHerdr()
    for name in ("create_pane", "start_agent", "prompt_agent", "agent_status",
                 "read_agent", "close_pane"):
        monkeypatch.setattr(herdr, name, getattr(f, name))
    monkeypatch.setattr(herdr, "current_pane_id", lambda: "self")
    monkeypatch.setattr(herdr, "current_session", lambda: "s1")
    monkeypatch.setattr(herdr, "current_workspace_id", lambda: "cw")
    monkeypatch.setattr(herdr, "ensure_workspace", lambda *a, **k: "w1")
    monkeypatch.setattr(herdr, "find_workspace", lambda *a, **k: None)
    return f


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def paths(tmp_path, fake):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    return Paths(tmp_path)


def _agent(paths, name, caps=("x",), maxc=1):
    d = paths.agent_dir(name)
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml.safe_dump(
        {"name": name, "capabilities": list(caps), "max_concurrent": maxc}), encoding="utf-8")
    (d / "prompt.md").write_text("persona", encoding="utf-8")


def _board(paths, *tasks):
    b = Board("g", "goal", list(tasks))
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def _rt(paths):
    f = paths.state_dir / "runtime.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _outbox(paths, agent, tid, data):
    d = paths.outbox(agent)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"g__{tid}.json"
    f.write_text(data if isinstance(data, str) else json.dumps({"task_id": tid, **data}),
                 encoding="utf-8")
    return f


def _ws(paths, agent, tid):
    from pathlib import Path
    return Path(resolve_task_workspace(load_manifest(paths, agent), "g", tid))


def _assert_reclaimed(paths, fake, agent, tid, pane):
    assert f"g:{tid}" not in _rt(paths)
    assert pane in fake.closed
    assert not (paths.inbox(agent) / f"g__{tid}.json").exists()
    assert not (paths.outbox(agent) / f"g__{tid}.json").exists()
    assert not _ws(paths, agent, tid).exists()


# ---------- 分派 ----------

def test_dispatch_then_done_reclaims_everything(paths, fake, tmp_path):
    _agent(paths, "w1")
    b = _board(paths, Task("t1", "do it", capabilities=["x"]))
    res = coordinator.tick(paths, b)
    assert res["dispatched"] == ["t1"]
    entry = _rt(paths)["g:t1"]
    assert (entry["agent"], entry["pane_id"], entry["herdr_name"], entry["session"]) == ("w1", "p1", "hp1", "s1")
    assert (paths.inbox("w1") / "g__t1.json").exists() and _ws(paths, "w1", "t1").is_dir()
    assert "g__t1.json" in fake.prompts[0][1] and fake.prompts[0][0] == "hp1"
    t = b.tasks["t1"]
    assert (t.status, t.attempts, t.tried_agents) == (DISPATCHED, 1, ["w1"])

    real = tmp_path / "out.md"
    real.write_text("r")
    _outbox(paths, "w1", "t1", {"status": "done", "summary": "ok",
                                "artifacts": [str(real), str(tmp_path / "missing.md")]})
    res = coordinator.tick(paths, b)
    assert res["changed"] == ["t1"] and res["complete"]
    assert t.status == DONE and t.result_summary == "ok"
    assert t.artifact_warnings == [str(tmp_path / "missing.md")]
    _assert_reclaimed(paths, fake, "w1", "t1", "p1")


def test_capacity_limits_per_agent(paths, fake):
    _agent(paths, "w1", maxc=1)
    b = _board(paths, Task("a", "g", capabilities=["x"]), Task("b", "g", capabilities=["x"]))
    res = coordinator.tick(paths, b)
    assert res["dispatched"] == ["a"] and res["waiting_for_agent"] == ["b"]


def test_fanout_group_gate(paths, fake):
    _agent(paths, "w1", maxc=5)
    b = _board(paths, Task("up", "g", status=DONE),
               Task("fan", "g {{shard}}", needs=["up"], capabilities=["x"], fanout={"max_parallel": 2}),
               Task("join", "g", needs=["fan"]))
    res = coordinator.expand_fanout(paths, b, "fan", ["g1", "g2", "g3"])
    assert res["ok"] and res["children"] == ["fan.1", "fan.2", "fan.3"]
    res = coordinator.tick(paths, b)
    assert res["dispatched"] == ["fan.1", "fan.2"] and res["waiting_for_capacity"] == ["fan.3"]


def test_failed_retry_switches_to_untried_agent(paths, fake):
    _agent(paths, "w1")
    _agent(paths, "w2")
    b = _board(paths, Task("t1", "g", capabilities=["x"], max_retries=1))
    coordinator.tick(paths, b)
    assert b.tasks["t1"].agent == "w1"
    _outbox(paths, "w1", "t1", {"status": "failed", "error": "boom"})
    res = coordinator.tick(paths, b)
    assert res["dispatched"] == ["t1"]
    t = b.tasks["t1"]
    assert (t.agent, t.tried_agents, t.attempts) == ("w2", ["w1", "w2"], 2)
    assert "p1" in fake.closed and set(_rt(paths)) == {"g:t1"}


def test_unroutable_is_stuck(paths, fake):
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["nobody"]))
    assert coordinator.is_stuck(b, paths)
    assert coordinator.run_loop(paths, dry_run=True, goal_id="g")["status"] == "stuck"


# ---------- monitor 无 outbox 判定 ----------

def _dispatched(paths, fake, *tasks):
    _agent(paths, "w1", maxc=5)
    b = _board(paths, *tasks)
    coordinator.tick(paths, b)
    return b


def test_idle_without_outbox_retries_after_grace(paths, fake, clock):
    b = _dispatched(paths, fake, Task("t1", "g", capabilities=["x"], max_retries=1))
    clock.tick(1)
    coordinator.monitor_once(paths, b)                    # working：刷新心跳
    fake.status["hp1"] = "idle"
    coordinator.monitor_once(paths, b)                    # 记 idle 锚点
    assert b.tasks["t1"].status == DISPATCHED
    clock.tick(11)
    assert coordinator.monitor_once(paths, b) == ["t1"]
    t = b.tasks["t1"]
    assert t.status == READY and "delivery missing" in t.error
    assert coordinator.retrying_tasks(paths, b)[0]["next_attempt"] == 2
    _assert_reclaimed(paths, fake, "w1", "t1", "p1")


def test_idle_malformed_outbox_is_terminal_and_quarantined(paths, fake, clock):
    b = _dispatched(paths, fake, Task("t1", "g", capabilities=["x"], max_retries=3))
    fake.status["hp1"] = "idle"
    _outbox(paths, "w1", "t1", "not json at all")
    coordinator.monitor_once(paths, b)
    clock.tick(11)
    coordinator.monitor_once(paths, b)
    assert b.tasks["t1"].status == FAILED and "malformed" in b.tasks["t1"].error
    assert len(list(paths.outbox("w1").glob("g__t1.malformed.*.json"))) == 1
    assert "g:t1" not in _rt(paths)


def test_working_with_fatal_signature_fails_without_retry(paths, fake):
    b = _dispatched(paths, fake, Task("t1", "g", capabilities=["x"], max_retries=3))
    fake.text["hp1"] = "Error: insufficient_quota for this key"
    coordinator.monitor_once(paths, b)
    t = b.tasks["t1"]
    assert t.status == FAILED and "fatal" in t.error and t.runs[-1]["outcome"] == "fatal"
    _assert_reclaimed(paths, fake, "w1", "t1", "p1")


def test_awaiting_input_then_answer(paths, fake):
    b = _dispatched(paths, fake, Task("t1", "g", capabilities=["x"]))
    fake.status["hp1"] = "idle"
    fake.text["hp1"] = "思考中…\n⏺ [[NEEDS-INPUT]] 用哪个分支？\n> "
    assert coordinator.monitor_once(paths, b) == ["t1"]
    assert b.tasks["t1"].status == AWAITING_INPUT
    aw = coordinator.awaiting_tasks(paths, b)
    assert aw == [{"task_id": "t1", "agent": "w1", "question": "用哪个分支？", "escalated": False}]
    assert coordinator.answer_task(paths, b, "t1", "main")["ok"]
    assert fake.prompts[-1] == ("hp1", "main") and b.tasks["t1"].status == DISPATCHED
    assert "awaiting_reason" not in _rt(paths)["g:t1"]


def test_ledger_lost_fails(paths, fake):
    b = _dispatched(paths, fake, Task("t1", "g", capabilities=["x"], max_retries=0))
    (paths.state_dir / "runtime.json").write_text("{}")
    coordinator.monitor_once(paths, b)
    assert b.tasks["t1"].status == FAILED and "ledger-lost" in b.tasks["t1"].error


# ---------- skip / retask / reject ----------

def test_bypass_inflight_reclaims_and_unblocks(paths, fake):
    b = _dispatched(paths, fake, Task("a", "g", capabilities=["x"]), Task("b", "g", needs=["a"]))
    res = coordinator.bypass_tasks(paths, b, ["a", "ghost"], reason="不需要")
    assert res["bypassed"] == ["a"] and res["not_found"] == ["ghost"] and res["newly_ready"] == ["b"]
    assert b.tasks["a"].status == BYPASSED and b.tasks["a"].result_summary == "不需要"
    _assert_reclaimed(paths, fake, "w1", "a", "p1")
    assert coordinator.bypass_tasks(paths, b, ["a"])["already_terminal"] == ["a"]


def test_retask_running_requires_force(paths, fake):
    b = _dispatched(paths, fake, Task("a", "原指令", capabilities=["x"]))
    assert coordinator.retask_task(paths, b, "a", append="补充")["rejected_running"] == ["a"]
    with pytest.raises(ValueError):
        coordinator.retask_task(paths, b, "a", new_goal="含 {{slot}}", force=True)
    assert "g:a" in _rt(paths)                             # 校验失败不回收
    res = coordinator.retask_task(paths, b, "a", append="补充", force=True)
    t = b.tasks["a"]
    assert res["forced"] and t.status == READY and t.attempts == 0 and t.goal == "原指令\n补充"
    assert t.runs[-1]["outcome"] == "retasked"
    _assert_reclaimed(paths, fake, "w1", "a", "p1")


def _impl_rev(paths, fake):
    b = _dispatched(paths, fake, Task("impl", "g", capabilities=["x"]),
                    Task("rev", "g", needs=["impl"], capabilities=["x"], reject_to=["impl"]))
    _outbox(paths, "w1", "impl", {"status": "done", "summary": "v1"})
    coordinator.tick(paths, b)
    assert b.tasks["impl"].status == DONE and b.tasks["rev"].status == DISPATCHED
    return b


def test_manual_reject_resets_target_and_descendants(paths, fake, tmp_path):
    b = _impl_rev(paths, fake)
    ev = tmp_path / "ev.txt"
    ev.write_text("证据")
    res = coordinator.apply_reject(paths, b, "impl", from_task="rev", reason="有 bug", evidence=[str(ev)])
    assert res["applied"] == ["impl"] and res["reset"] == ["impl", "rev"] and res["reject_count"] == 1
    impl, rev = b.tasks["impl"], b.tasks["rev"]
    assert impl.status == READY and rev.status == PENDING and impl.attempts == 0
    assert impl.result_summary == "" and impl.rejections[0]["reason"] == "有 bug"
    stashed = impl.rejections[0]["evidence"][0]
    assert "/.rejections/r0/" in stashed and open(stashed).read() == "证据"
    _assert_reclaimed(paths, fake, "w1", "rev", "p2")
    assert coordinator.apply_reject(paths, b, "impl", from_task="nope")["invalid_from"]


def test_auto_reject_exhausted_parks_rejecter(paths, fake):
    b = _impl_rev(paths, fake)
    b.tasks["impl"].reject_count = coordinator.MAX_REJECTS
    _outbox(paths, "w1", "rev", {"status": "reject", "reject_to": "impl", "summary": "还是不行"})
    coordinator.monitor_once(paths, b)
    rev = b.tasks["rev"]
    assert rev.status == AWAITING_INPUT and "reject-exhausted" in rev.error
    assert _rt(paths)["g:rev"]["escalated"] is True and "p2" in fake.closed
    assert not (paths.outbox("w1") / "g__rev.json").exists()
    assert coordinator.awaiting_tasks(paths, b)[0]["escalated"] is True
    assert coordinator.answer_task(paths, b, "rev", "x")["escalated"]
    coordinator.monitor_once(paths, b)                    # parked：不再做存活/超时判定
    assert rev.status == AWAITING_INPUT
    res = coordinator.apply_reject(paths, b, "impl", from_task="rev", force=True)
    assert res["applied"] == ["impl"] and rev.status == PENDING and _rt(paths) == {}


def test_auto_reject_to_illegal_target_completes_rejecter(paths, fake):
    b = _impl_rev(paths, fake)
    _outbox(paths, "w1", "rev", {"status": "reject", "reject_to": "ghost", "summary": "?"})
    coordinator.monitor_once(paths, b)
    rev = b.tasks["rev"]
    assert rev.status == DONE and "打回目标非法" in rev.error and rev.runs[-1]["outcome"] == "reject_invalid"
    _assert_reclaimed(paths, fake, "w1", "rev", "p2")


# ---------- 账本回收 ----------

def test_close_goal_panes_only_touches_goal(paths, fake):
    _agent(paths, "w1")
    ws = _ws(paths, "w1", "t")
    ws.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "runtime.json").write_text(json.dumps({
        "g:t": {"goal_id": "g", "task_id": "t", "agent": "w1", "pane_id": "p9"},
        "g:d": {"goal_id": "g", "task_id": "d", "agent": "w1", "pane_id": "<dry-run>"},
        "other:t": {"goal_id": "other", "task_id": "t", "agent": "w1", "pane_id": "p8"}}))
    assert coordinator.close_goal_panes(paths, "g") == 1
    assert fake.closed == ["p9"] and not ws.exists() and list(_rt(paths)) == ["other:t"]


def test_reconcile_reaps_orphans_but_protects_live_sessions(paths, fake):
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "runtime.json").write_text(json.dumps({
        "g:dead": {"agent": "w1", "pane_id": "p1", "herdr_name": "hdead"},
        "g:live": {"agent": "w1", "pane_id": "p2", "herdr_name": "hlive"},
        "g:dry": {"agent": "w1", "pane_id": "<dry-run>"}}))
    fake.status.update(hdead="gone", hlive="idle")
    assert coordinator.reconcile_ledger(paths) == 1
    assert fake.closed == ["p1"] and set(_rt(paths)) == {"g:live", "g:dry"}
