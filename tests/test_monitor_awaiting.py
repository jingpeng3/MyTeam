"""monitor：等人(AWAITING_INPUT)任务遇 herdr 查询失败(unknown)时，只按 awaiting 上限判，不按心跳陈旧度误杀。

herdr 打桩（同 test_coordinator 的 FakeHerdr 思路），其余账本/信号/board 走真实代码。
"""
from __future__ import annotations

import json
import time

import pytest
import yaml

from agent_team import coordinator, herdr
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, AWAITING_INPUT, FAILED


class FakeHerdr:
    def __init__(self):
        self.n = 0
        self.status: dict[str, str] = {}
        self.text: dict[str, str] = {}
        self.closed: list[str] = []
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
