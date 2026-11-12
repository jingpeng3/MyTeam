"""monitor：等人(AWAITING_INPUT)任务遇 herdr 查询失败(unknown)时，只按 awaiting 上限判，不按心跳陈旧度误杀。

herdr 打桩（同 test_coordinator 的 FakeHerdr 思路），其余账本/信号/board 走真实代码。
"""
from __future__ import annotations

import json
import time

import pytest
import yaml

from agent_team import coordinator, herdr, interventions, monitor
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, AWAITING_INPUT, DISPATCHED, FAILED


class FakeHerdr:
    def __init__(self):
        self.n = 0
        self.status: dict[str, str] = {}
        self.text: dict[str, str] = {}
        self.closed: list[str] = []
        self.dismissed: list[str] = []
        self.raise_status = False      # True 时 agent_status 抛 HerdrError（模拟 herdr 抖动）

    def create_pane(self, *a, **k):
        self.n += 1
        return f"p{self.n}"

    def start_agent(self, pane_id, *a, **k):
        return f"h{pane_id}"

    def prompt_agent(self, target, text, *a, **k):
        pass

    def agent_status(self, target):
        if self.raise_status:
            raise herdr.HerdrError("herdr hiccup")
        return self.status.get(target, "working")

    def read_agent(self, target, lines=120, session=None, live=False):
        return self.text.get(target, "")

    def dismiss_dialog(self, target, timeout_s=15.0):
        self.dismissed.append(target)
        self.status[target] = "idle"
        return True

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
                 "read_agent", "close_pane", "dismiss_dialog"):
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


def _rt(paths):
    f = paths.state_dir / "runtime.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _awaiting(paths, fake, clock):
    """分派一个任务并让它进入 AWAITING_INPUT（idle + 提问标记），之后 herdr 查询开始失败。"""
    d = paths.agent_dir("w1")
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml.safe_dump(
        {"name": "w1", "capabilities": ["x"], "max_concurrent": 1}), encoding="utf-8")
    (d / "prompt.md").write_text("persona", encoding="utf-8")
    b = Board("g", "goal", [Task("t1", "g", capabilities=["x"], max_retries=0)])
    b.validate()
    b.refresh_ready()
    b.save(paths)
    coordinator.tick(paths, b)
    fake.status["hp1"] = "idle"
    fake.text["hp1"] = "⏺ [[NEEDS-INPUT]] 用哪个分支？\n> "
    coordinator.monitor_once(paths, b)
    assert b.tasks["t1"].status == AWAITING_INPUT
    entry = _rt(paths)["g:t1"]
    assert entry["awaiting_since"] == clock.t and entry.get("last_alive_at", clock.t) <= clock.t
    fake.raise_status = True
    return b


def test_unknown_status_while_awaiting_does_not_fail_on_stale_heartbeat(paths, fake, clock):
    # 心跳与 awaiting_since 都已陈旧 1000s（> 默认 900s 交付超时，< 3600s 等人上限）：不得误判静默死亡
    b = _awaiting(paths, fake, clock)
    clock.tick(1000)
    assert coordinator.monitor_once(paths, b) == []
    t = b.tasks["t1"]
    assert t.status == AWAITING_INPUT and not t.error
    assert "p1" not in fake.closed
    entry = _rt(paths)["g:t1"]
    assert entry["awaiting_reason"] == "用哪个分支？"


def test_unknown_status_while_awaiting_still_times_out_on_awaiting_limit(paths, fake, clock):
    # 超过等人上限：与 idle 分支的 awaiting 超时同一判据、同一收尾
    b = _awaiting(paths, fake, clock)
    clock.tick(coordinator.AWAITING_INPUT_TIMEOUT + 400)
    assert coordinator.monitor_once(paths, b) == ["t1"]
    t = b.tasks["t1"]
    assert t.status == FAILED and "awaiting human input timeout" in t.error
    assert t.runs[-1]["outcome"] == "timeout"
    assert "p1" in fake.closed and "g:t1" not in _rt(paths)


def _events(paths):
    f = paths.state_dir / "run.log"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]


def test_answer_then_monitor_does_not_remark_same_question(paths, fake, clock):
    # answer 后 pane 尾部旧哨兵还没滚出：下一轮 monitor 不得再标 awaiting、也不得判交付缺失
    b = _awaiting(paths, fake, clock)
    fake.raise_status = False                     # herdr 正常，pane 仍是同一个提问
    out = interventions.answer_task(paths, b, "t1", "用 main")
    assert out["ok"]
    clock.tick(0.111)                             # 复现 run.log 里 0.111s 后重扫
    assert coordinator.monitor_once(paths, b) == []
    assert b.tasks["t1"].status == DISPATCHED and not b.tasks["t1"].error
    kinds = [e["event"] for e in _events(paths)]
    assert kinds.count("task_awaiting_input") == 1 and "task_answered" in kinds
    assert "p1" not in fake.closed


def test_answer_grace_expires_and_same_question_is_real_again(paths, fake, clock):
    # 宽限过后同一句仍挂在那儿（worker 没恢复）：仍按新提问标 awaiting，避免旧答复永久遮蔽
    b = _awaiting(paths, fake, clock)
    fake.raise_status = False
    assert interventions.answer_task(paths, b, "t1", "用 main")["ok"]
    clock.tick(monitor.ANSWER_REPROMPT_GRACE + 1)
    coordinator.monitor_once(paths, b)
    assert b.tasks["t1"].status == AWAITING_INPUT
    assert [e["event"] for e in _events(paths)].count("task_awaiting_input") == 2


def _dispatched(paths):
    d = paths.agent_dir("w1")
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml.safe_dump(
        {"name": "w1", "capabilities": ["x"], "max_concurrent": 1}), encoding="utf-8")
    (d / "prompt.md").write_text("persona", encoding="utf-8")
    b = Board("g", "goal", [Task("t1", "g", capabilities=["x"], max_retries=0)])
    b.validate()
    b.refresh_ready()
    b.save(paths)
    coordinator.tick(paths, b)
    return b


def test_blocked_dialog_is_awaiting_not_silent_death(paths, fake, clock):
    # herdr 报 blocked（AskUserQuestion 表单等）：标等人，超过交付超时也不当静默死亡回收
    b = _dispatched(paths)
    fake.status["hp1"] = "blocked"
    fake.text["hp1"] = "选哪个方案？\n❯ 1. A\n  2. B\nEnter to select · Esc to cancel"
    assert coordinator.monitor_once(paths, b) == ["t1"]
    assert b.tasks["t1"].status == AWAITING_INPUT
    assert _rt(paths)["g:t1"]["awaiting_reason"] == "选哪个方案？"
    clock.tick(coordinator.DEFAULT_TASK_TIMEOUT + 100)
    assert coordinator.monitor_once(paths, b) == []
    assert b.tasks["t1"].status == AWAITING_INPUT and "p1" not in fake.closed
    assert [e["event"] for e in _events(paths)].count("task_awaiting_input") == 1


def test_blocked_without_question_uses_generic_reason(paths, fake, clock):
    b = _dispatched(paths)
    fake.status["hp1"] = "blocked"
    fake.text["hp1"] = "❯ Accept\n  Decline\nEsc to cancel"
    coordinator.monitor_once(paths, b)
    assert b.tasks["t1"].status == AWAITING_INPUT
    assert _rt(paths)["g:t1"]["awaiting_reason"] == monitor.BLOCKED_DIALOG_REASON


def test_blocked_still_times_out_on_awaiting_limit(paths, fake, clock):
    b = _dispatched(paths)
    fake.status["hp1"] = "blocked"
    coordinator.monitor_once(paths, b)
    clock.tick(coordinator.AWAITING_INPUT_TIMEOUT + 1)
    assert coordinator.monitor_once(paths, b) == ["t1"]
    assert b.tasks["t1"].status == FAILED and "awaiting human input timeout" in b.tasks["t1"].error


def test_answer_blocked_dismisses_dialog_first(paths, fake, clock, monkeypatch):
    b = _dispatched(paths)
    fake.status["hp1"] = "blocked"
    coordinator.monitor_once(paths, b)
    prompts = []
    monkeypatch.setattr(herdr, "prompt_agent",
                        lambda target, text, *a, **k: prompts.append((target, text, fake.status[target])))
    out = interventions.answer_task(paths, b, "t1", "选 A")
    assert out["ok"] and fake.dismissed == ["hp1"]
    assert prompts == [("hp1", "选 A", "idle")]    # 弹窗关掉之后才注入答复
    assert b.tasks["t1"].status == DISPATCHED


def test_answer_blocked_reports_when_dialog_wont_close(paths, fake, clock, monkeypatch):
    b = _dispatched(paths)
    fake.status["hp1"] = "blocked"
    coordinator.monitor_once(paths, b)
    monkeypatch.setattr(herdr, "dismiss_dialog", lambda target, timeout_s=15.0: False)
    out = interventions.answer_task(paths, b, "t1", "选 A")
    assert not out["ok"] and "blocked" in out["error"]
    assert b.tasks["t1"].status == AWAITING_INPUT
