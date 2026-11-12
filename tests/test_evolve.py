"""team-evolve 的确定性部分：start 选题、分组校验、worktree/门禁、评审记录、合并、收尾回写。

全部走真实代码与真实 git（tmp 仓库），不拉 herdr、不跑 pytest/ruff（门禁一律 skip_slow）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent_team import cli, evolve, evolve_ops, retro
from agent_team.config import Paths, atomic_write_json


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path
    for d in ("coordinator/plans", "coordinator/insights", "coordinator/state/jobs", "shared/artifacts"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "agent_team").mkdir()
    (root / "tests").mkdir()
    (root / "agent_team" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (root / "agent_team" / "other.py").write_text("Y = 1\n", encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (root / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    # 子 agent 提示词模板随 skill 入库；测试项目里放一份，供 render_prompt 读取
    shutil.copytree(Path(__file__).parent.parent / ".claude" / "skills" / "team-evolve" / "templates",
                    root / ".claude" / "skills" / "team-evolve" / "templates")
    (root / ".gitignore").write_text(
        "shared/artifacts/*\ncoordinator/state/\ncoordinator/insights/backlog.json\n.worktrees/\n*.lock\n",
        encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.chdir(root)
    return Paths(root)


def _seed(paths: Paths, ids: list[str]) -> None:
    now = time.time()
    items = []
    for iid in ids:
        items.append({
            "id": iid, "title": f"建议 {iid}", "scope": "project", "category": "framework",
            "target": "agent_team/mod.py", "problem": "有问题", "proposal": "改掉",
            "evidence": ["run.log:1 证据"], "impact": "medium", "effort": "low", "confidence": 0.9,
            "status": "open", "occurrences": 1, "sources": ["test"], "first_seen": now, "last_seen": now,
            "note": "", "updated_at": now,
        })
    atomic_write_json(retro.backlog_file(paths), {"version": 1, "ingested": {}, "items": items})


def _analysis(groups: list[dict], *, merges=None, dismissed=None) -> dict:
    return {"summary": "s", "merges": merges or [], "dismissed": dismissed or [], "groups": groups}


def _g(name: str, ids: list[str], files: list[str], deps: list[str] | None = None) -> dict:
    return {"name": name, "title": f"组 {name}", "plan": "按建议改", "insight_ids": ids,
            "files": files, "depends_on": deps or []}


def _start(paths: Paths, ids: list[str], rid: str = "ev-1") -> dict:
    return evolve.start(paths, ids, run_id=rid)


def _grouped(paths: Paths, rid: str, groups: list[dict], **kw) -> dict:
    f = evolve.run_dir(paths, rid) / "analysis.json"
    f.write_text(json.dumps(_analysis(groups, **kw), ensure_ascii=False), encoding="utf-8")
    return evolve.apply_analysis(paths, rid, f)


def _develop(paths: Paths, rid: str, group: str, files: dict[str, str], ids: list[str],
             msg: str | None = None, report: dict | None = None) -> None:
    c = evolve.load_run(paths, rid)
    wt = Path(c["groups"][group]["worktree"])
    for rel, text in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", msg or f"evolve({group}): 落地建议")
    rep = report or {"summary": "做完了", "changes": [{
        "insight_ids": ids, "title": "t", "files": list(files), "evidence": ["run.log:1"],
        "change": "改了", "verification": "pytest"}]}
    (evolve.group_dir(paths, rid, group) / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    evolve.record_developer(paths, rid, group)


def _review(paths: Paths, rid: str, group: str, n: int, verdict: str, findings=None) -> None:
    d = evolve.group_dir(paths, rid, group)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"review-r{n}.json").write_text(json.dumps(
        {"verdict": verdict, "head": "x", "summary": "s", "findings": findings or []}, ensure_ascii=False),
        encoding="utf-8")


# ---------------- start ----------------

def test_start_selects_open_and_writes_evidence(proj):
    _seed(proj, ["ins-a", "ins-b"])
    c = _start(proj, ["ins-a", "ins-b"])
    assert c["insight_ids"] == ["ins-a", "ins-b"] and c["status"] == "started"
    d = evolve.run_dir(proj, "ev-1")
    assert (d / "evidence" / "insights.md").exists() and (d / "evidence" / "insights.json").exists()
    assert _git(proj.root, "rev-parse", "--short", "HEAD") == c["base_commit"]


def test_start_rejects_missing_non_open_and_too_many(proj):
    _seed(proj, [f"ins-{i}" for i in range(13)])
    with pytest.raises(ValueError, match="不存在或不是 open"):
        evolve.start(proj, ["ins-ghost"], run_id="x1")
    with pytest.raises(ValueError, match="最多选"):
        evolve.start(proj, [f"ins-{i}" for i in range(13)], run_id="x2")
    retro.mark(proj, ["ins-0"], "applied", note="done")
    with pytest.raises(ValueError, match="不存在或不是 open"):
        evolve.start(proj, ["ins-0"], run_id="x3")


def test_start_requires_clean_worktree(proj):
    _seed(proj, ["ins-a"])
    (proj.root / "agent_team" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="未提交改动"):
        evolve.start(proj, ["ins-a"], run_id="ev-dirty")


# ---------------- group 校验 ----------------

def test_group_persists_and_rejects_double_apply(proj):
    _seed(proj, ["ins-a", "ins-b", "ins-c"])
    _start(proj, ["ins-a", "ins-b", "ins-c"])
    c = _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"]),
                                _g("g2", ["ins-c"], ["tests/test_mod.py"])],
                 merges=[{"insight_ids": ["ins-b"], "into": "ins-a", "reason": "同一问题"}])
    assert set(c["groups"]) == {"g1", "g2"} and c["status"] == "grouped"
    assert (evolve.run_dir(proj, "ev-1") / "evidence" / "g1.md").exists()
    with pytest.raises(ValueError, match="已经分组过"):
        _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"])])


@pytest.mark.parametrize("analysis, want", [
    ([_g("g1", ["ins-a"], ["agent_team/"])], "没有分配到"),                       # ins-b 漏分配
    ([_g("g1", ["ins-a"], ["agent_team/mod.py"]), _g("g2", ["ins-b"], ["agent_team/"])], "相交"),
    ([_g("g1", ["ins-a", "ins-b"], ["agent_team/mod.py"]),
      _g("g2", ["ins-b"], ["tests/test_mod.py"])], "同时被分到"),
    ([_g("g1", ["ins-a"], ["../escape.py"]), _g("g2", ["ins-b"], ["tests/test_mod.py"])], "非法路径"),
    ([_g("g1", ["ins-a"], ["coordinator/"]), _g("g2", ["ins-b"], ["tests/test_mod.py"])], "非法路径"),
    ([_g("g1", ["ins-a"], ["agent_team/mod.py"], ["g2"]),
      _g("g2", ["ins-b"], ["tests/test_mod.py"], ["g1"])], "环"),
    ([_g("g1", ["ins-a"], ["agent_team/mod.py"], ["ghost"]),
      _g("g2", ["ins-b"], ["tests/test_mod.py"])], "不存在的组"),
])
def test_group_validation_rejections(proj, analysis, want):
    _seed(proj, ["ins-a", "ins-b"])
    run = _start(proj, ["ins-a", "ins-b"])
    with pytest.raises(ValueError, match=want):
        evolve.validate_analysis(run, _analysis(analysis))


def test_group_rejects_bad_dismissed_and_merge_targets(proj):
    _seed(proj, ["ins-a", "ins-b"])
    run = _start(proj, ["ins-a", "ins-b"])
    with pytest.raises(ValueError, match="没写理由"):
        evolve.validate_analysis(run, _analysis([_g("g1", ["ins-a"], ["agent_team/mod.py"])],
                                                dismissed=[{"insight_id": "ins-b", "reason": ""}]))
    with pytest.raises(ValueError, match="合并到它自己"):
        evolve.validate_analysis(run, _analysis([_g("g1", ["ins-a"], ["agent_team/mod.py"])],
                                                merges=[{"insight_ids": ["ins-b"], "into": "ins-b"}]))
    with pytest.raises(ValueError, match="被驳回"):
        evolve.validate_analysis(run, _analysis([_g("g1", ["ins-a"], ["agent_team/mod.py"])],
                                                merges=[{"insight_ids": ["ins-a"], "into": "ins-b"}],
                                                dismissed=[{"insight_id": "ins-b", "reason": "不做"}]))


def test_group_caps_at_six(proj):
    ids = [f"ins-{i}" for i in range(13)]
    _seed(proj, ids)
    run = _start(proj, ids[:12])
    groups = [_g(f"g{i}", [ids[i]], [f"agent_team/f{i}.py"]) for i in range(7)]
    with pytest.raises(ValueError, match="最多分 6 组"):
        evolve.validate_analysis(run, _analysis(groups))


# ---------------- worktree / 门禁 / 记录 ----------------

def test_worktree_gate_and_preview(proj):
    _seed(proj, ["ins-a"])
    _start(proj, ["ins-a"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py", "tests/test_mod.py"])])
    wt = evolve_ops.create_worktree(proj, "ev-1", "g1")
    assert Path(wt["worktree"]).exists() and wt["branch"] == "evolve/ev-1/g1"
    _develop(proj, "ev-1", "g1", {"agent_team/mod.py": "X = 2\n"}, ["ins-a"])
    preview = evolve_ops.gate_group(proj, "ev-1", "g1", skip_slow=True, preview=True)
    assert preview["ok"], [c for c in preview["checks"] if not c["ok"]]
    assert not list(evolve.group_dir(proj, "ev-1", "g1").glob("gate-r*.json"))
    res = evolve_ops.gate_group(proj, "ev-1", "g1", skip_slow=True)
    assert res["ok"] and res["round"] == 1 and Path(res["file"]).exists()
    assert evolve.load_run(proj, "ev-1")["groups"]["g1"]["status"] == "gated"


def test_gate_rejects_out_of_scope_prefix_and_report(proj):
    _seed(proj, ["ins-a", "ins-b"])
    _start(proj, ["ins-a", "ins-b"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py", "tests/test_mod.py"]),
                            _g("g2", ["ins-b"], ["agent_team/other.py"])])
    evolve_ops.create_worktree(proj, "ev-1", "g1")
    _develop(proj, "ev-1", "g1", {"agent_team/other.py": "Y = 9\n"}, ["ins-a"],
             msg="tweak stuff",
             report={"summary": "", "changes": [{"insight_ids": ["ins-b"], "title": "", "files": ["x"],
                                                 "evidence": [], "change": "", "verification": ""}]})
    res = evolve_ops.gate_group(proj, "ev-1", "g1", skip_slow=True)
    bad = {c["name"]: c for c in res["checks"] if not c["ok"]}
    assert {"scope", "commit_prefix", "report_contract"} <= set(bad)
    assert any("组外文件" in p for p in bad["report_contract"]["detail"])
    assert any("不属于本组" in p for p in bad["report_contract"]["detail"])
    assert any("未被任何改动覆盖" in p for p in bad["report_contract"]["detail"])
    assert evolve.load_run(proj, "ev-1")["groups"]["g1"]["status"] == "gate_failed"


def test_record_developer_requires_worktree(proj):
    _seed(proj, ["ins-a"])
    _start(proj, ["ins-a"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"])])
    with pytest.raises(ValueError, match="还没有 worktree"):
        evolve.record_developer(proj, "ev-1", "g1")


# ---------------- 评审与修复轮 ----------------

def test_review_pass_with_blocking_rejected_and_fix_round_cap(proj):
    _seed(proj, ["ins-a"])
    _start(proj, ["ins-a"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py", "tests/test_mod.py"])])
    evolve_ops.create_worktree(proj, "ev-1", "g1")
    _develop(proj, "ev-1", "g1", {"agent_team/mod.py": "X = 2\n"}, ["ins-a"])
    evolve_ops.gate_group(proj, "ev-1", "g1", skip_slow=True)
    _review(proj, "ev-1", "g1", 1, "PASS", [{"severity": "blocking", "title": "没改彻底", "status": "open"}])
    with pytest.raises(ValueError, match="blocking"):
        evolve.record_review(proj, "ev-1", "g1")
    _review(proj, "ev-1", "g1", 2, "FAIL", [{"severity": "major", "title": "建议补测试", "status": "open"}])
    evolve.record_review(proj, "ev-1", "g1")
    assert evolve.load_run(proj, "ev-1")["groups"]["g1"]["status"] == "changes_requested"
    evolve.log(proj, "ev-1", "fix_round", group="g1")
    evolve.log(proj, "ev-1", "fix_round", group="g1")
    with pytest.raises(ValueError, match="abandoned"):
        evolve.log(proj, "ev-1", "fix_round", group="g1")
    _review(proj, "ev-1", "g1", 3, "PASS")
    evolve.record_review(proj, "ev-1", "g1")
    assert evolve.load_run(proj, "ev-1")["groups"]["g1"]["status"] == "approved"


def test_prompts_render_without_placeholders(proj):
    _seed(proj, ["ins-a"])
    _start(proj, ["ins-a"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"])])
    evolve_ops.create_worktree(proj, "ev-1", "g1")
    az = evolve.render_prompt(proj, "ev-1", "analyzer")
    assert "{{" not in az and "ins-a" in az and "analysis.json" in az
    for role in ("developer", "reviewer"):
        text = evolve.render_prompt(proj, "ev-1", role, "g1")
        assert "{{" not in text
        assert "evolve/ev-1/g1" in text and "agent_team/mod.py" in text


# ---------------- 合并 ----------------

def _ready_group(proj, rid: str, group: str, ids: list[str], file: str, content: str) -> None:
    evolve_ops.create_worktree(proj, rid, group)
    _develop(proj, rid, group, {file: content}, ids)
    assert evolve_ops.gate_group(proj, rid, group, skip_slow=True)["ok"]
    _review(proj, rid, group, 1, "PASS")
    evolve.record_review(proj, rid, group)


def test_merge_only_approved_gated_in_dependency_order(proj):
    _seed(proj, ["ins-a", "ins-b", "ins-c"])
    _start(proj, ["ins-a", "ins-b", "ins-c"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"]),
                            _g("g2", ["ins-b"], ["agent_team/other.py"], ["g1"]),
                            _g("g3", ["ins-c"], ["tests/test_mod.py"])])
    _ready_group(proj, "ev-1", "g1", ["ins-a"], "agent_team/mod.py", "X = 2\n")
    _ready_group(proj, "ev-1", "g2", ["ins-b"], "agent_team/other.py", "Y = 2\n")
    evolve.log(proj, "ev-1", "abandoned", group="g3", reason="证据不实")
    res = evolve_ops.merge(proj, "ev-1", skip_slow=True)
    assert res["merged"] == ["g1", "g2"] and res["conflict"] is None
    assert res["main_gate"]["ok"], [c for c in res["main_gate"]["checks"] if not c["ok"]]
    assert (proj.root / "agent_team" / "mod.py").read_text() == "X = 2\n"
    assert "g3" in evolve.load_run(proj, "ev-1")["groups"]
    assert not Path(res["cleaned"][0]).exists()
    branches = _git(proj.root, "branch", "--format=%(refname:short)")
    assert "evolve/ev-1/g1" not in branches and "evolve/ev-1/g3" not in branches


def test_merge_skips_group_whose_dependency_not_merged(proj):
    _seed(proj, ["ins-a", "ins-b"])
    _start(proj, ["ins-a", "ins-b"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"]),
                            _g("g2", ["ins-b"], ["agent_team/other.py"], ["g1"])])
    _ready_group(proj, "ev-1", "g2", ["ins-b"], "agent_team/other.py", "Y = 2\n")
    evolve.log(proj, "ev-1", "abandoned", group="g1", reason="证据不实")
    res = evolve_ops.merge(proj, "ev-1", skip_slow=True)
    assert res["merged"] == [] and res["main_gate"] is None
    g2 = evolve.load_run(proj, "ev-1")["groups"]["g2"]
    assert g2["status"] == "abandoned" and "未合并" in g2["reason"]
    assert not (proj.root / "agent_team" / "other.py").read_text().startswith("Y = 2")


def test_merge_stops_on_conflict(proj):
    _seed(proj, ["ins-a", "ins-b"])
    _start(proj, ["ins-a", "ins-b"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"]),
                            _g("g2", ["ins-b"], ["agent_team/other.py"])])
    _ready_group(proj, "ev-1", "g1", ["ins-a"], "agent_team/mod.py", "X = 2\n")
    _ready_group(proj, "ev-1", "g2", ["ins-b"], "agent_team/other.py", "Y = 2\n")
    # 绕过边界制造真实冲突：两个分支同时改同一个文件
    for g, val in (("g1", "conflict A\n"), ("g2", "conflict B\n")):
        wt = Path(evolve.load_run(proj, "ev-1")["groups"][g]["worktree"])
        (wt / "agent_team" / "mod.py").write_text(val, encoding="utf-8")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"evolve({g}): 冲突")
    res = evolve_ops.merge(proj, "ev-1", skip_slow=True)
    assert res["conflict"]["group"] in ("g1", "g2") and res["main_gate"] is None
    assert res["cleaned"] == []
    assert _git(proj.root, "status", "--porcelain", "--untracked-files=no") == ""   # 已 abort，不留冲突现场


# ---------------- 收尾 ----------------

def test_finish_marks_applied_dismissed_and_keeps_open(proj):
    _seed(proj, ["ins-a", "ins-b", "ins-c", "ins-d"])
    _start(proj, ["ins-a", "ins-b", "ins-c", "ins-d"])
    _grouped(proj, "ev-1", [_g("g1", ["ins-a"], ["agent_team/mod.py"]),
                            _g("g2", ["ins-c"], ["agent_team/other.py"])],
             merges=[{"insight_ids": ["ins-b"], "into": "ins-a", "reason": "同一问题"}],
             dismissed=[{"insight_id": "ins-d", "reason": "证据不实"}])
    _ready_group(proj, "ev-1", "g1", ["ins-a"], "agent_team/mod.py", "X = 2\n")
    evolve.log(proj, "ev-1", "abandoned", group="g2", reason="做不了")
    evolve_ops.merge(proj, "ev-1", skip_slow=True)
    dry = evolve.finish(proj, "ev-1", dry_run=True)
    assert dry["applied_ids"] == ["ins-a"]
    assert retro.ranked(proj) and all(it["status"] == "open" for it in retro.ranked(proj, status=None))
    res = evolve.finish(proj, "ev-1")
    assert res["applied_ids"] == ["ins-a"]
    assert sorted(res["dismissed_ids"]) == ["ins-b", "ins-d"]
    by = {it["id"]: it for it in retro.ranked(proj, status=None)}
    assert by["ins-a"]["status"] == "applied" and "merge" in by["ins-a"]["note"]
    assert by["ins-b"]["status"] == "dismissed" and "合并" in by["ins-b"]["note"]
    assert by["ins-c"]["status"] == "open" and "未落地" in by["ins-c"]["note"]
    assert by["ins-d"]["status"] == "dismissed" and "证据不实" in by["ins-d"]["note"]
    d = evolve.run_dir(proj, "ev-1")
    assert (d / "result.json").exists() and (d / "report.md").exists()
    assert evolve.load_run(proj, "ev-1")["status"] == "done"


def test_cli_status_smoke(proj, capsys):
    _seed(proj, ["ins-a"])
    _start(proj, ["ins-a"])
    assert cli.main(["evolve", "status", "--run", "ev-1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == "ev-1" and out["insight_ids"] == ["ins-a"]
