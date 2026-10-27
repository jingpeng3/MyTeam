"""agent.yaml 列表字段容错：null / 标量 / 列表都归一成 list，不再拆成单字符或抛 TypeError。"""
from __future__ import annotations

import pytest

from agent_team import registry
from agent_team.config import Paths, load_manifest
from agent_team.models import Runtime, _as_list


def _write_agent(paths: Paths, name: str, body: str) -> None:
    adir = paths.agent_dir(name)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path)
    p.team_file.write_text("defaults:\n  runtime:\n    cli: claude\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("v, want", [
    (None, []), ("python", ["python"]), (["a", "b"], ["a", "b"]), (("a",), ["a"]), (3, [3]),
])
def test_as_list(v, want):
    assert _as_list(v) == want


def test_null_fields_become_empty(paths):
    _write_agent(paths, "w", "name: w\ncapabilities:\nskills:\nhidden_skills:\n"
                             "runtime:\n  flags:\n")
    m = load_manifest(paths, "w")
    assert m.capabilities == [] and m.skills == [] and m.hidden_skills == []
    assert m.runtime.flags == []


def test_scalar_fields_not_split_into_chars(paths):
    _write_agent(paths, "w", "name: w\ncapabilities: python\nskills: s1\nhidden_skills: h1\n"
                             "runtime:\n  flags: --dangerously-skip-permissions\n")
    m = load_manifest(paths, "w")
    assert m.capabilities == ["python"] and m.skills == ["s1"] and m.hidden_skills == ["h1"]
    assert m.runtime.flags == ["--dangerously-skip-permissions"]
    assert m.matches(["python"]) == 1


def test_list_fields_unchanged(paths):
    _write_agent(paths, "w", "name: w\ncapabilities: [python, web]\n"
                             "runtime:\n  flags: [--a, --b]\n")
    m = load_manifest(paths, "w")
    assert m.capabilities == ["python", "web"]
    assert m.runtime.flags == ["--a", "--b"]


def test_runtime_merge_scalar_base_flags():
    assert Runtime.merge({"flags": "--x"}, {}).flags == ["--x"]
    assert Runtime.merge({"flags": None}, {"flags": None}).flags == []


def test_list_agents_keeps_null_capabilities_agent(paths):
    _write_agent(paths, "w", "name: w\ncapabilities:\n")
    assert [m.name for m in registry.list_agents(paths)] == ["w"]
