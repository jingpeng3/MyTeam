"""Board 的 DAG 逻辑：结构校验、ready/级联跳过、fan-out 展开、终态判定。纯内存，不落盘。"""
from __future__ import annotations

import pytest

from agent_team.board import Board, DagError
from agent_team.models import (Task, PENDING, READY, DONE, FAILED, SKIPPED, BYPASSED,
                               AWAITING_EXPANSION, AWAITING_INPUT)


def _b(*tasks: Task) -> Board:
    return Board("g", "goal", list(tasks))


# ---------- validate ----------

def test_validate_ok_chain():
    _b(Task("a", "g"), Task("b", "g", needs=["a"]),
       Task("c", "g", needs=["b"], see_artifacts=["b"], reject_to=["a"])).validate()


def test_validate_dangling_need():
    with pytest.raises(DagError, match="悬空依赖"):
        _b(Task("a", "g", needs=["ghost"])).validate()


def test_validate_see_artifacts_must_be_subset_of_needs():
    with pytest.raises(DagError, match="see_artifacts"):
        _b(Task("a", "g"), Task("b", "g"), Task("c", "g", needs=["a"], see_artifacts=["b"])).validate()


def test_validate_reject_to_must_be_ancestor():
    with pytest.raises(DagError, match="reject_to"):
        _b(Task("a", "g"), Task("b", "g"), Task("c", "g", needs=["a"], reject_to=["b"])).validate()


def test_validate_cycle():
    with pytest.raises(DagError, match="环"):
        _b(Task("a", "g", needs=["b"]), Task("b", "g", needs=["a"])).validate()


# ---------- refresh_ready / 级联 ----------

def test_refresh_ready_marks_roots_ready_and_stamps_ready_at():
    b = _b(Task("a", "g"), Task("b", "g", needs=["a"]))
    assert b.refresh_ready() == []
    assert b.tasks["a"].status == READY and b.tasks["a"].ready_at is not None
    assert b.tasks["b"].status == PENDING


def test_refresh_ready_bypassed_counts_as_satisfied():
    b = _b(Task("a", "g", status=BYPASSED), Task("b", "g", needs=["a"]))
    b.refresh_ready()
    assert b.tasks["b"].status == READY


def test_refresh_ready_cascades_skip_with_root():
    b = _b(Task("a", "g", status=FAILED), Task("b", "g", needs=["a"]), Task("c", "g", needs=["b"]))
    assert sorted(b.refresh_ready()) == ["b", "c"]
    assert b.tasks["c"].status == SKIPPED and "root=a" in b.tasks["c"].error
    assert b.root_failure(b.tasks["c"]) == "a"


def test_fanout_template_goes_awaiting_expansion():
    b = _b(Task("up", "g", status=DONE), Task("fan", "g", needs=["up"], fanout={"max_parallel": 2}))
    b.refresh_ready()
    assert b.tasks["fan"].status == AWAITING_EXPANSION
    assert [t.task_id for t in b.expandable_tasks()] == ["fan"]


def test_ancestors_and_descendants():
    b = _b(Task("a", "g"), Task("b", "g", needs=["a"]), Task("c", "g", needs=["b"]), Task("d", "g"))
    assert b.ancestors("c") == {"a", "b"}
    assert b.descendants("a") == {"b", "c"}
    assert b.descendants("d") == set()


# ---------- expand_fanout ----------

def _fan_board() -> Board:
    b = _b(Task("up", "g", status=DONE),
           Task("fan", "g", needs=["up"], capabilities=["x"], fanout={"max_parallel": 2}, max_retries=3),
           Task("join", "g", needs=["fan"], see_artifacts=["fan"]))
    b.refresh_ready()
    return b


def test_expand_fanout_creates_children_and_rewires():
    b = _fan_board()
    res = b.expand_fanout("fan", ["g1", "g2", "g3"], ["e1", "e2", "e3"])
    assert res == {"ok": True, "children": ["fan.1", "fan.2", "fan.3"], "rewired": ["join"]}
    c2 = b.tasks["fan.2"]
    assert (c2.goal, c2.expect, c2.needs, c2.capabilities) == ("g2", "e2", ["up"], ["x"])
    assert c2.fanout is None and c2.fanout_group == "fan" and c2.fanout_max_parallel == 2
    assert c2.max_retries == 3
    assert b.tasks["join"].needs == ["fan.1", "fan.2", "fan.3"]
    assert b.tasks["join"].see_artifacts == ["fan.1", "fan.2", "fan.3"]
    assert b.tasks["fan"].status == BYPASSED
    b.validate()


@pytest.mark.parametrize("tid, goals, expects, err", [
    ("nope", ["g"], None, "不存在"),
    ("up", ["g"], None, "不是 fan-out"),
    ("fan", [], None, "为空"),
    ("fan", ["a", "b"], ["e"], "长度"),
])
def test_expand_fanout_rejects_bad_input(tid, goals, expects, err):
    res = _fan_board().expand_fanout(tid, goals, expects)
    assert not res["ok"] and err in res["error"]


def test_expand_fanout_twice_rejected():
    b = _fan_board()
    assert b.expand_fanout("fan", ["a"])["ok"]
    assert "终态" in b.expand_fanout("fan", ["a"])["error"]


# ---------- 终态 / 失败 ----------

def test_is_complete_and_has_failures():
    assert _b(Task("a", "g", status=DONE), Task("b", "g", status=BYPASSED)).is_complete()
    assert not _b(Task("a", "g", status=DONE), Task("b", "g", status=AWAITING_INPUT)).is_complete()
    assert _b(Task("a", "g", status=SKIPPED)).has_failures()
    assert not _b(Task("a", "g", status=BYPASSED)).has_failures()


def test_summary_counts():
    assert _b(Task("a", "g", status=DONE), Task("b", "g", status=DONE),
              Task("c", "g")).summary() == "done=2 pending=1"


def test_task_post_init_validation():
    with pytest.raises(ValueError, match="状态非法"):
        Task("a", "g", status="weird")
    with pytest.raises(ValueError, match="非负整数"):
        Task("a", "g", max_retries=-1)
    with pytest.raises(ValueError, match="max_parallel"):
        Task("a", "g", fanout={"max_parallel": 0})
