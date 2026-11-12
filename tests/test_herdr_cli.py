"""herdr 集成层的 CLI 参数拼装：_run 打桩记录 argv，不碰真实 herdr。"""
from __future__ import annotations

import pytest

from agent_team import herdr

_REAL_NOTIFY = herdr.notify      # 导入时取真实实现：conftest 的 autouse 打桩发生在 fixture 阶段

class _Calls(list):
    def __init__(self):
        super().__init__()
        self.responses: dict[str, object] = {}


@pytest.fixture
def calls(monkeypatch):
    """_run 打桩：记录 argv；responses[子命令] 可给返回值或异常。"""
    rec = _Calls()
    responses = rec.responses

    def run(args, timeout=60, session=None):
        rec.append(list(args))
        r = responses.get(args[1]) if len(args) > 1 else None
        if isinstance(r, Exception):
            raise r
        return r if r is not None else {}

    monkeypatch.setattr(herdr, "_run", run)
    return rec


def test_state_sets_only_contain_real_herdr_states():
    # herdr 只上报 idle/working/blocked/done/unknown，agent 不在时归一为 gone
    real = {"idle", "working", "blocked", "done", "unknown", "gone"}
    groups = [herdr.ALIVE_STATES, herdr.READY_STATES, herdr.BLOCKED_STATES, herdr.DEAD_STATES]
    assert set().union(*groups) <= real
    assert "blocked" in herdr.BLOCKED_STATES and "unknown" not in set().union(*groups)


def test_prompt_until_implies_wait(calls):
    # herdr 拒绝不带 --wait 的 --until
    herdr.prompt_agent("a", "hi", until=["blocked"])
    assert calls[-1] == ["agent", "prompt", "a", "hi", "--wait", "--until", "blocked"]
    herdr.prompt_agent("a", "hi")
    assert calls[-1] == ["agent", "prompt", "a", "hi"]


def test_submit_task_prompt_waits_for_start_and_returns_session(calls):
    calls.responses["prompt"] = {"type": "agent_prompted",
                                 "agent": {"agent_status": "working",
                                           "agent_session": {"kind": "id", "value": "sess-1"}}}
    assert herdr.submit_task_prompt("a", "go") == "sess-1"
    assert calls[-1] == ["agent", "prompt", "a", "go", "--wait", "--until", "working",
                         "--until", "blocked", "--timeout", str(herdr.PROMPT_START_TIMEOUT_MS)]


def test_submit_task_prompt_without_session_returns_empty(calls):
    calls.responses["prompt"] = {"agent": {"agent_status": "working"}}
    assert herdr.submit_task_prompt("a", "go") == ""


def test_submit_task_prompt_stalled_raises(calls):
    calls.responses["prompt"] = herdr.HerdrError("stalled", code="agent_prompt_stalled")
    with pytest.raises(herdr.HerdrError):
        herdr.submit_task_prompt("a", "go")


def test_wait_agent_ready_uses_server_side_wait(calls):
    assert herdr._wait_agent_ready("a", timeout_s=2)
    assert calls[-1] == ["agent", "wait", "a", "--until", "idle", "--until", "done",
                         "--timeout", "2000"]
    calls.responses["wait"] = herdr.HerdrError("timeout", code="timeout")
    assert not herdr._wait_agent_ready("a", timeout_s=2)


def test_dismiss_dialog_sends_esc_then_waits(calls):
    assert herdr.dismiss_dialog("a")
    assert calls[0] == ["agent", "send-keys", "a", "esc"] and calls[1][:2] == ["agent", "wait"]


def test_notify_swallows_errors(monkeypatch):
    rec = []

    def run(args, timeout=60, session=None):
        rec.append(args)
        raise herdr.HerdrError("no server")

    monkeypatch.setattr(herdr, "_run", run)
    _REAL_NOTIFY("t", "b", sound="request")     # conftest 已把 herdr.notify 打桩，这里测真实实现
    assert rec == [["notification", "show", "t", "--sound", "request", "--body", "b"]]
