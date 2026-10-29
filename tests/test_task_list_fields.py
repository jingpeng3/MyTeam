"""Task 列表字段归一/校验：null→[]、单字符串→单元素列表、非法类型报错、task_id 引用保序去重。"""
from __future__ import annotations

import json

import pytest

from agent_team import cli
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task


def test_none_becomes_empty_list():
    t = Task(task_id="a", goal="x", needs=None, capabilities=None, context_refs=None,
             rejections=None, runs=None)
    assert t.needs == [] and t.capabilities == [] and t.context_refs == []
    assert t.rejections == [] and t.runs == []


def test_string_wrapped_not_split():
    t = Task(task_id="a", goal="x", capabilities="py",
             context_refs="shared/artifacts/x.md", needs="b")
    assert t.capabilities == ["py"]
    assert t.context_refs == ["shared/artifacts/x.md"]
    assert t.needs == ["b"]


def test_blank_strings_treated_as_empty():
    t = Task(task_id="a", goal="x", capabilities="", needs="  ", context_refs=["r", " ", ""])
    assert t.capabilities == [] and t.needs == [] and t.context_refs == ["r"]
    assert Task(task_id="a", goal="x", needs=["b", ""]).needs == ["b"]


def test_tuple_becomes_list():
    assert Task(task_id="a", goal="x", needs=("b", "c")).needs == ["b", "c"]


@pytest.mark.parametrize("fld, val", [
    ("needs", 5),
    ("capabilities", {"py": True}),
    ("context_refs", 1.5),
    ("see_artifacts", True),
    ("rejections", "oops"),          # 元素为对象的字段不接受字符串包装
    ("runs", {"attempt": 1}),
])
def test_bad_type_raises(fld, val):
    with pytest.raises(ValueError, match=f"字段 {fld} 必须是列表"):
        Task(task_id="a", goal="x", **{fld: val})


def test_dedup_preserves_order():
    t = Task(task_id="d", goal="x", needs=["b", "a", "b", "a"],
             see_artifacts=["a", "a"], reject_to=["b", "a", "b"],
             context_refs=["r", "r"])
    assert t.needs == ["b", "a"]
    assert t.see_artifacts == ["a"]
    assert t.reject_to == ["b", "a"]
    assert t.context_refs == ["r", "r"]        # 非 task_id 引用字段不去重


def test_duplicate_needs_passes_validate():
    b = Board("g", "goal", [Task(task_id="a", goal="x"),
                            Task(task_id="b", goal="y", needs=["a", "a"])])
    b.validate()                               # 旧行为：入度 2 → 误报「检测到环」
    b.refresh_ready()
    assert b.tasks["a"].status == "ready"


def test_board_roundtrip_from_disk(tmp_path):
    p = Paths(tmp_path)
    b = Board("g", "goal", [Task(task_id="a", goal="x", capabilities=["py"]),
                            Task(task_id="b", goal="y", needs=["a"], see_artifacts=["a"])])
    b.save(p)
    loaded = Board.load(p, "g")
    assert loaded is not None
    assert loaded.tasks["b"].needs == ["a"] and loaded.tasks["a"].capabilities == ["py"]


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _plan(proj, tasks):
    f = proj / "p.json"
    f.write_text(json.dumps({"goal_id": "demo", "goal": "g", "tasks": tasks},
                            ensure_ascii=False), encoding="utf-8")
    return str(f)


def test_plan_null_needs_and_dup_needs_load(proj):
    f = _plan(proj, [{"task_id": "a", "goal": "x", "capabilities": "py", "needs": None},
                     {"task_id": "b", "goal": "y", "capabilities": ["py"], "needs": ["a", "a"]}])
    assert cli.main(["plan", f]) == 0
    b = Board.load(Paths(proj), "demo")
    assert b.tasks["a"].needs == [] and b.tasks["a"].capabilities == ["py"]
    assert b.tasks["b"].needs == ["a"]


def test_plan_blank_capabilities_and_needs_load_with_nocap_warning(proj, capsys):
    f = _plan(proj, [{"task_id": "a", "goal": "x", "capabilities": "", "needs": ""}])
    assert cli.main(["plan", f]) == 0
    t = Board.load(Paths(proj), "demo").tasks["a"]
    assert t.capabilities == [] and t.needs == []
    assert "未声明 capabilities" in capsys.readouterr().err


def test_plan_bad_list_type_one_line_error(proj, capsys):
    f = _plan(proj, [{"task_id": "a", "goal": "x", "needs": 3}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["plan", f])
    err = capsys.readouterr().err
    assert ei.value.code == 2 and "plan 非法" in err and "字段 needs 必须是列表" in err
    assert "Traceback" not in err
