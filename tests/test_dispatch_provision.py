"""provisioning 失败：事件带原因（stage/error），同一轮分派内有限短退避重试（ins-cb25244c）。

herdr 打桩为可按 label 注入失败的内存假实现（写法同 test_dispatch_pinned），退避 sleep 打桩为
只记录不等待，账本/信号/board 落盘走真实代码。
"""
from __future__ import annotations

import json
import threading

import pytest
import yaml

from agent_team import dispatch, herdr, ledger
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DISPATCHED, FAILED, READY, runtime_key


class FakeHerdr:
    """fail[stage] = 需要失败的 task_id → 剩余失败次数（-1 = 一直失败）。"""

    def __init__(self):
        self.n = 0
        self.labels: list[str] = []
        self.calls: dict[str, int] = {"create_pane": 0, "start_agent": 0, "prompt": 0}
        self.fail: dict[str, dict[str, int]] = {"create_pane": {}, "start_agent": {}, "prompt": {}}
        self.closed: list[str] = []
        self._lock = threading.Lock()

    def _maybe_fail(self, stage: str, label: str) -> None:
        with self._lock:
            self.calls[stage] += 1
            tid = label.split(":")[2]
            left = self.fail[stage].get(tid, 0)
            if left == 0:
                return
            if left > 0:
                self.fail[stage][tid] = left - 1
        raise herdr.HerdrError(f"{stage} boom")

    def create_pane(self, wid, cwd, label, *a, **k):
        self._maybe_fail("create_pane", label)
        with self._lock:
            self.n += 1
            return f"p{self.n}"

    def start_agent(self, pane_id, label, *a, **k):
        with self._lock:
            self.labels.append(label)
        self._maybe_fail("start_agent", label)
        return f"h{pane_id}|{label}"

    def prompt_agent(self, target, *a, **k):
        self._maybe_fail("prompt", target.split("|", 1)[1])

    def close_pane(self, pane_id):
        self.closed.append(pane_id)


@pytest.fixture
def fake(monkeypatch):
    f = FakeHerdr()
    for name in ("create_pane", "start_agent", "prompt_agent", "close_pane"):
        monkeypatch.setattr(herdr, name, getattr(f, name))
    monkeypatch.setattr(herdr, "current_session", lambda: "s1")
    monkeypatch.setattr(herdr, "ensure_workspace", lambda *a, **k: "w1")
    return f


@pytest.fixture
def sleeps(monkeypatch):
    """退避 sleep 打桩：只记录时长，绝不真实等待。"""
    rec: list[float] = []
    monkeypatch.setattr(dispatch, "_sleep", rec.append)
    return rec


@pytest.fixture
def paths(tmp_path, fake, sleeps):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    return Paths(tmp_path)


def _agent(paths, name, caps=("x",), maxc=2):
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


def _events(paths, name):
    f = paths.state_dir / "run.log"
    if not f.exists():
        return []
    recs = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in recs if r.get("event") == name]


def _round(paths, b):
    return dispatch.run_dispatch_round(paths, [b], dry_run=False)["g"]


@pytest.mark.parametrize("stage", ["create_pane", "start_agent", "prompt"])
def test_provision_failed_event_carries_stage_and_error(paths, fake, stage):
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    fake.fail[stage]["t"] = -1
    assert _round(paths, b)["dispatch_errors"] == ["t"]
    ev = _events(paths, "dispatch_provision_failed")
    assert [(e["provision_attempt"], e["round_retry"]) for e in ev] == [(0, 0), (0, 1)]   # 同轮重试了一次
    for e in ev:
        assert (e["stage"], e["error"], e["agent"]) == (stage, f"{stage} boom", "w1")
        # create_pane 失败时没拿到 pane：不写 pane_id；其后环节失败带上已建的 pane
        assert ("pane_id" in e) == (stage != "create_pane")
    [retry] = _events(paths, "provision_retry")
    assert retry["stage"] == stage and f"{stage} failed: {stage} boom" == retry["error"]
    # 同轮重试不吃跨 tick 预算：本轮失败只计 1 次
    assert (retry["round_retries"], retry["next_retry"], retry["provision_attempt"]) == (1, "next_tick", 1)


def test_transient_failure_recovered_within_same_round(paths, fake, sleeps):
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    fake.fail["start_agent"]["t"] = 1                                # 首次失败、第二次成功
    res = _round(paths, b)
    t = b.tasks["t"]
    assert res["dispatched"] == ["t"] and res["dispatch_errors"] == []
    assert (t.status, t.agent, t.provision_attempts, t.attempts) == (DISPATCHED, "w1", 0, 1)
    assert sleeps == [dispatch.PROVISION_BACKOFF_S[0]]
    assert len(fake.labels) == 2 and len(set(fake.labels)) == 2      # 每次 infra 尝试都用新 label
    assert [lb.rsplit(":", 1)[1] for lb in fake.labels] == ["a1p0r0", "a1p0r1"]
    assert fake.closed == ["p1"]                                     # 失败那次的 pane 已关
    assert _events(paths, "provision_retry") == []
    [d] = _events(paths, "dispatched")
    assert d["round_retries"] == 1
    assert ledger.load(paths)[runtime_key("g", "t")]["pane_id"] == "p2"


def test_persistent_failure_exhausts_only_across_rounds(paths, fake):
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    fake.fail["start_agent"]["t"] = -1
    t = b.tasks["t"]
    mx, per_round = dispatch.PROVISION_MAX_RETRIES, 1 + dispatch.PROVISION_ROUND_RETRIES
    for n in range(1, mx + 1):                                       # 前 MAX 个失败 tick：每轮只 +1，仍 READY
        _round(paths, b)
        assert (t.status, t.provision_attempts, t.agent) == (READY, n, "")
        assert _events(paths, "dispatch_error") == []
    _round(paths, b)                                                 # 第 MAX+1 个失败 tick 才 FAILED（与基线一致）
    assert t.status == FAILED
    assert t.error == f"provisioning failed after {mx + 1} rounds: start_agent failed: start_agent boom"
    [err] = _events(paths, "dispatch_error")
    assert (err["kind"], err["stage"], err["error"]) == (
        "provision_exhausted", "start_agent", "start_agent failed: start_agent boom")
    _round(paths, b)                                                 # FAILED 不再被派
    assert fake.calls["start_agent"] == (mx + 1) * per_round        # herdr 调用上限 (MAX+1)×(1+ROUND_RETRIES)
    assert len(set(fake.labels)) == len(fake.labels)


def test_failures_across_two_ticks_then_success_dispatches(paths, fake):
    # 证据场景（run.log:2616-2621）：故障持续两个 tick，第三个 tick 恢复 → 必须派出，不能提前 FAILED
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    per_round = 1 + dispatch.PROVISION_ROUND_RETRIES
    fake.fail["start_agent"]["t"] = 2 * per_round
    t = b.tasks["t"]
    assert _round(paths, b)["dispatch_errors"] == ["t"]
    assert _round(paths, b)["dispatch_errors"] == ["t"]
    assert (t.status, t.provision_attempts) == (READY, 2)
    assert _round(paths, b)["dispatched"] == ["t"]
    assert (t.status, t.provision_attempts, t.agent) == (DISPATCHED, 0, "w1")
    assert _events(paths, "dispatch_error") == []
    assert len(set(fake.labels)) == len(fake.labels) == 2 * per_round + 1


def test_zero_round_budget_disables_same_round_retry(paths, fake, sleeps, monkeypatch):
    monkeypatch.setattr(dispatch, "PROVISION_ROUND_BUDGET_S", 0)
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    fake.fail["create_pane"]["t"] = -1
    _round(paths, b)
    assert sleeps == [] and fake.calls["create_pane"] == 1
    assert b.tasks["t"].provision_attempts == 1
    [retry] = _events(paths, "provision_retry")
    assert retry["round_retries"] == 0


def test_round_budget_measured_from_round_start(paths, fake, sleeps, monkeypatch):
    # 整轮起点 t=0，之后时钟恒为 19：剩余 1s 不够 2s 退避 → 不再同轮重试。若 deadline 改成线程内
    # 起算（19+20），这里会同轮重试并派出，用例即失败。
    calls = []

    def clock():
        calls.append(1)
        return 0.0 if len(calls) == 1 else 19.0

    monkeypatch.setattr(dispatch, "_clock", clock)
    monkeypatch.setattr(dispatch, "PROVISION_ROUND_BUDGET_S", 20.0)
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    fake.fail["start_agent"]["t"] = 1
    res = _round(paths, b)
    assert res["dispatch_errors"] == ["t"] and sleeps == [] and fake.calls["start_agent"] == 1
    assert (b.tasks["t"].status, b.tasks["t"].provision_attempts) == (READY, 1)
    assert _events(paths, "dispatch_error") == []
    [retry] = _events(paths, "provision_retry")
    assert retry["round_retries"] == 0


def test_concurrent_tasks_isolated_and_state_written_on_main_thread(paths, fake, sleeps, monkeypatch):
    writers: list[str] = []
    orig_board_save, orig_ledger_save = Board.save, ledger.save

    def board_save(self, *a, **k):
        writers.append(threading.current_thread().name)
        return orig_board_save(self, *a, **k)

    def ledger_save(*a, **k):
        writers.append(threading.current_thread().name)
        return orig_ledger_save(*a, **k)

    monkeypatch.setattr(Board, "save", board_save)
    monkeypatch.setattr(ledger, "save", ledger_save)
    _agent(paths, "w1", maxc=2)
    b = _board(paths, Task("a", "g", capabilities=["x"]), Task("b", "g", capabilities=["x"]))
    writers.clear()
    fake.fail["start_agent"]["b"] = -1
    res = _round(paths, b)
    assert res["dispatched"] == ["a"] and res["dispatch_errors"] == ["b"]
    ta, tb = b.tasks["a"], b.tasks["b"]
    assert (ta.status, ta.provision_attempts) == (DISPATCHED, 0)
    assert (tb.status, tb.provision_attempts) == (READY, 1)
    assert sleeps == [dispatch.PROVISION_BACKOFF_S[0]]               # 只有失败的那个在退避
    assert writers and set(writers) == {threading.main_thread().name}
    assert [v["task_id"] for v in ledger.load(paths).values()] == ["a"]


def test_serial_dispatch_task_shares_retry_wrapper(paths, fake, sleeps):
    _agent(paths, "w1")
    m = dispatch.load_manifest(paths, "w1")
    b = _board(paths, Task("ok", "g", capabilities=["x"]), Task("bad", "g", capabilities=["x"]))
    fake.fail["start_agent"]["ok"] = 1
    fake.fail["start_agent"]["bad"] = -1
    assert dispatch._try_dispatch(paths, b, b.tasks["ok"], m, dry_run=False) is True
    assert (b.tasks["ok"].status, b.tasks["ok"].provision_attempts) == (DISPATCHED, 0)
    assert dispatch._try_dispatch(paths, b, b.tasks["bad"], m, dry_run=False) is False
    assert (b.tasks["bad"].status, b.tasks["bad"].provision_attempts) == (READY, 1)
    [retry] = _events(paths, "provision_retry")
    assert (retry["task_id"], retry["round_retries"], retry["next_retry"]) == ("bad", 1, "next_tick")
    assert len(sleeps) == 2


def test_non_provision_error_after_retry_reports_round_failures(paths, monkeypatch):
    calls = []

    def fake_provision(*a, **k):
        calls.append((k["provision_attempt"], k["round_retry"]))
        if len(calls) == 1:
            raise dispatch.ProvisionError("create_pane failed: x", stage="create_pane")
        raise OSError("disk full")

    monkeypatch.setattr(dispatch, "_provision_herdr", fake_provision)
    t = Task("t", "g")
    with pytest.raises(OSError) as ei:
        dispatch._provision_with_retry(paths, "w1", None, "g", t, [], None, deadline=float("inf"))
    assert calls == [(0, 0), (0, 1)] and ei.value.round_failures == 2


def test_dispatch_records_agent_session(paths, fake, monkeypatch):
    # herdr 上报的 worker 会话 ID 写进账本与 dispatched 事件，复盘据此定位 transcript
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    monkeypatch.setattr(herdr, "prompt_agent", lambda target, text, **k: {
        "agent": {"agent_status": "working", "agent_session": {"value": "sess-9"}}})
    assert _round(paths, b)["dispatched"] == ["t"]
    assert ledger.load(paths)[runtime_key("g", "t")]["agent_session"] == "sess-9"
    [ev] = _events(paths, "dispatched")
    assert ev["agent_session"] == "sess-9"


def test_dispatch_prompt_waits_for_turn_start(paths, fake, monkeypatch):
    # 分派引导 prompt 必须带 --wait --until working/blocked：没开始执行就当场失败走 provision 重试
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    seen = []
    monkeypatch.setattr(herdr, "prompt_agent", lambda target, text, **k: seen.append(k) or {})
    assert _round(paths, b)["dispatched"] == ["t"]
    assert seen[0]["until"] == ["working", "blocked"] and seen[0]["timeout_ms"] > 0
    assert "agent_session" not in ledger.load(paths)[runtime_key("g", "t")]
