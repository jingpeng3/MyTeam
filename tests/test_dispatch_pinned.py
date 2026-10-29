"""select_agent 对已指定 agent（pin）的任务：同样过容量闸；pin 的 agent 不在时回退按能力选人。

herdr 打桩为内存假实现（同 test_coordinator），其余账本/信号/board 落盘走真实代码。
"""
from __future__ import annotations

import json

import pytest
import yaml

from agent_team import coordinator, herdr
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DISPATCHED, READY


class FakeHerdr:
    def __init__(self):
        self.n = 0

    def create_pane(self, *a, **k):
        self.n += 1
        return f"p{self.n}"

    def start_agent(self, pane_id, *a, **k):
        return f"h{pane_id}"

    def prompt_agent(self, *a, **k):
        pass

    def agent_status(self, target):
        return "working"

    def read_agent(self, *a, **k):
        return ""

    def close_pane(self, pane_id):
        pass


@pytest.fixture
def paths(tmp_path, monkeypatch):
    f = FakeHerdr()
    for name in ("create_pane", "start_agent", "prompt_agent", "agent_status",
                 "read_agent", "close_pane"):
        monkeypatch.setattr(herdr, name, getattr(f, name))
    monkeypatch.setattr(herdr, "current_pane_id", lambda: "self")
    monkeypatch.setattr(herdr, "current_session", lambda: "s1")
    monkeypatch.setattr(herdr, "current_workspace_id", lambda: "cw")
    monkeypatch.setattr(herdr, "ensure_workspace", lambda *a, **k: "w1")
    monkeypatch.setattr(herdr, "find_workspace", lambda *a, **k: None)
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


def _events(paths, name):
    f = paths.state_dir / "run.log"
    if not f.exists():
        return []
    recs = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in recs if r.get("event") == name]


def test_pinned_tasks_respect_max_concurrent(paths):
    _agent(paths, "tester", maxc=1)
    _agent(paths, "other", maxc=5)           # 有空闲的同能力 agent，也不应抢走 pin 的任务
    b = _board(paths, Task("a", "g", capabilities=["x"], agent="tester"),
               Task("b", "g", capabilities=["x"], agent="tester"))
    res = coordinator.tick(paths, b)
    assert res["dispatched"] == ["a"] and res["waiting_for_agent"] == ["b"]
    assert (b.tasks["a"].status, b.tasks["a"].agent) == (DISPATCHED, "tester")
    assert (b.tasks["b"].status, b.tasks["b"].agent) == (READY, "tester")


def test_pinned_to_missing_agent_falls_back(paths):
    _agent(paths, "w1")
    b = _board(paths, Task("t", "g", capabilities=["x"], agent="ghost"))
    res = coordinator.tick(paths, b)         # 不应抛 FileNotFoundError
    assert res["dispatched"] == ["t"]
    assert (b.tasks["t"].status, b.tasks["t"].agent) == (DISPATCHED, "w1")
    ev = _events(paths, "pinned_agent_missing")
    assert [(e["task_id"], e["agent"]) for e in ev] == [("t", "ghost")]
