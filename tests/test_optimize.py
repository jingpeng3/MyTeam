"""流程优化 campaign：collect（时间窗/模板归属/指标/信号/边界）、日志与状态、门禁、合并、框架问题、历史与报告。

全部走真实代码与真实 git（tmp 仓库），不拉 herdr、不跑 pytest/ruff（门禁一律 skip_slow）。
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from agent_team import cli, optimize as opt, optimize_ops as ops, optimize_report, retro
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DONE

# 2026-10-26（周一）~ 11-01（周日）是 today=11-02 时的「上一个自然周」
TODAY = date(2026, 11, 2)


def _ts(day: str, hour: int = 10) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").timestamp()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _agent(root: Path, name: str, caps: list[str]) -> None:
    d = root / "agents" / name
    (d / "workspace").mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml.safe_dump({"name": name, "capabilities": caps}), encoding="utf-8")
    (d / "prompt.md").write_text(f"# {name}\n", encoding="utf-8")


def _plan(root: Path, name: str, tasks: list[dict], slots: list[str] | None = None) -> None:
    data = {"goal_id": name, "routing": {"when": "x", "required_slots": slots or []},
            "goal": "做 " + " ".join("{{" + s + "}}" for s in slots or []), "tasks": tasks}
    (root / "coordinator" / "plans" / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _job(paths: Paths, gid: str, spec: list[tuple[str, str, list[dict]]], day: str, wall: float = 1000.0,
         errors: dict | None = None) -> Board:
    """spec: [(task_id, agent, runs)]，runs 每条至少含 active_s/outcome。"""
    start = _ts(day)
    tasks = []
    for tid, agent, runs in spec:
        t = Task(tid, f"goal {tid}", capabilities=["c"], agent=agent, status=DONE,
                 error=(errors or {}).get(tid))
        t.runs = [{"attempt": i + 1, "agent": agent, "started_at": start, "ended_at": start + r["active_s"],
                   "awaiting_s": r.get("awaiting_s", 0.0), "active_s": r["active_s"],
                   "wall_s": r["active_s"], "outcome": r.get("outcome", "done")} for i, r in enumerate(runs)]
        tasks.append(t)
    b = Board(gid, f"目标 {gid}", tasks)
    b.created_at, b.completed_at = start, start + wall
    b.save(paths)
    return b


def _events(paths: Paths, events: list[dict]) -> None:
    f = paths.state_dir / "run.log"
    with open(f, "a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path
    (root / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    for d in ("coordinator/plans", "coordinator/insights", "coordinator/state/jobs", "shared/artifacts"):
        (root / d).mkdir(parents=True, exist_ok=True)
    _agent(root, "scout", ["s"])
    _agent(root, "builder", ["b"])
    _agent(root, "op", ["o"])
    _plan(root, "digest", [{"task_id": "scan", "goal": "扫 {{q}}", "capabilities": ["s"]},
                           {"task_id": "build", "goal": "建", "capabilities": ["b"], "needs": ["scan"]}], ["q"])
    _plan(root, "tabbed", [{"task_id": "scan", "goal": "扫", "capabilities": ["s"]},
                           {"task_id": "news", "goal": "新闻", "capabilities": ["s"]},
                           {"task_id": "page", "goal": "页", "capabilities": ["b"], "needs": ["scan", "news"]}])
    _plan(root, "operate", [{"task_id": "run", "goal": "操作", "capabilities": ["o"]}])
    _plan(root, "quiet", [{"task_id": "solo", "goal": "单步", "capabilities": ["o"]}])
    (root / ".gitignore").write_text("shared/artifacts/*\ncoordinator/state/\n.worktrees/\n*.lock\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.chdir(root)
    return Paths(root)


def _seed_week(paths: Paths) -> None:
    # digest：两次，build 是热点、一次被打回
    _job(paths, "dg-1026", [("scan", "scout", [{"active_s": 200}]), ("build", "builder", [{"active_s": 600}])],
         "2026-10-26")
    _job(paths, "dg-1027", [("scan", "scout", [{"active_s": 300}]),
                            ("build", "builder", [{"active_s": 400}, {"active_s": 300}])], "2026-10-27")
    _events(paths, [{"ts": _ts("2026-10-27", 11), "event": "task_rejected", "goal_id": "dg-1027",
                     "task_id": "build", "from_task": "build", "reason": "缺字段"}])
    # operate：寿命回收 + 人工续跑 + 中断误判（框架信号）+ 拉起失败（框架信号）
    _job(paths, "op-1028", [("run", "op", [{"active_s": 5400, "outcome": "timeout"}, {"active_s": 340}])],
         "2026-10-28", wall=6000)
    _events(paths, [
        {"ts": _ts("2026-10-28", 11), "event": "task_timeout", "goal_id": "op-1028", "task_id": "run",
         "reason": "exceeded max lifetime 5402s (> 5400s)"},
        {"ts": _ts("2026-10-28", 12), "event": "task_retasked", "goal_id": "op-1028", "task_id": "run",
         "mode": "append", "reason": "续跑"},
        {"ts": _ts("2026-10-28", 12), "event": "task_awaiting_input", "goal_id": "op-1028", "task_id": "run",
         "question": "⎿  Interrupted · What should Claude do instead?"},
        {"ts": _ts("2026-10-28", 12), "event": "dispatch_provision_failed", "goal_id": "op-1028",
         "task_id": "run", "pane_id": "w:p1", "provision_attempt": 0},
    ])
    # quiet：健康、无信号
    _job(paths, "qt-1029", [("solo", "op", [{"active_s": 60}])], "2026-10-29", wall=100)
    # 窗口外与复盘 Job：不计入
    _job(paths, "dg-1102", [("scan", "scout", [{"active_s": 1}]), ("build", "builder", [{"active_s": 1}])],
         "2026-11-02")
    _job(paths, "retro-dg-1026-1", [("analyze-run", "scout", [{"active_s": 5}])], "2026-10-27")
    # 临时 plan：只记录不优化
    _job(paths, "adhoc-1", [("weird", "scout", [{"active_s": 50, "outcome": "failed"}])], "2026-10-30")


def _collect(paths: Paths, **kw):
    return opt.collect(paths, opt.resolve_window(today=TODAY), cid="opt-t", **kw)


# ---------------- 时间窗 ----------------

def test_window_defaults_to_last_natural_week():
    w = opt.resolve_window(today=TODAY)
    assert (w["start"], w["end"], w["label"]) == ("2026-10-26", "2026-11-01", "上一个自然周")
    assert opt.resolve_window(today=date(2026, 11, 8))["start"] == "2026-10-26"   # 周日仍算上一周


def test_window_days_and_explicit_range():
    assert opt.resolve_window(days=7, today=TODAY)["start"] == "2026-10-27"
    w = opt.resolve_window(since="2026-10-01", until="2026-10-03", today=TODAY)
    assert (w["start"], w["end"]) == ("2026-10-01", "2026-10-03")
    with pytest.raises(ValueError):
        opt.resolve_window(since="2026-10-05", until="2026-10-01", today=TODAY)


# ---------------- collect ----------------

def test_collect_attributes_jobs_and_filters_window(proj):
    _seed_week(proj)
    c = _collect(proj)
    f = c["flows"]
    assert f["digest"]["jobs"] == ["dg-1026", "dg-1027"]          # 窗口外 dg-1102 不计
    assert f["operate"]["jobs"] == ["op-1028"]
    assert opt.ADHOC in f and f[opt.ADHOC]["status"] == "skipped"   # 临时 plan 只记录
    assert not any(g.startswith("retro-") for fl in f.values() for g in fl["jobs"])
    assert f["quiet"]["status"] == "skipped" and "健康" in f["quiet"]["reason"]
    assert c["selected"][0] == "operate"                          # 寿命回收 3 + 人工改写 2 > 打回 2 + 热点
    assert set(c["selected"]) == {"operate", "digest"}


def test_collect_metrics_spans_and_signals(proj):
    _seed_week(proj)
    c = _collect(proj)
    dg = c["flows"]["digest"]
    build = next(t for t in dg["metrics"]["tasks"] if t["task_id"] == "build")
    assert build["span_p50_s"] == 650.0 and build["runs"] == 3     # 返工两次运行累加进关键路径
    assert dg["metrics"]["totals"]["rejects"] == 1
    assert dg["signal_counts"]["reject"] == 1 and dg["signal_counts"]["hotspot"] >= 1
    ev = (opt.campaign_dir(proj, "opt-t") / "evidence" / "digest.md").read_text(encoding="utf-8")
    assert "run.log:" in ev and "缺字段" in ev and "agents/builder/prompt.md" in ev


def test_framework_signals_do_not_score_flow_but_become_candidates(proj):
    _seed_week(proj)
    c = _collect(proj)
    kinds = {x["kind"]: x for x in c["framework_candidates"]}
    assert set(kinds) == {"interrupt_misread", "provision_failure"}
    assert kinds["interrupt_misread"]["flows"] == ["operate"]
    # 只有流程类信号计分：寿命回收 3 + 人工改写 2 + 热点 1
    assert c["flows"]["operate"]["score"] == 6


def test_awaiting_reentry_detected_after_answer(proj):
    _job(proj, "op-a", [("run", "op", [{"active_s": 100}])], "2026-10-28", wall=400)
    t0 = _ts("2026-10-28", 11)
    _events(proj, [{"ts": t0, "event": "task_answered", "goal_id": "op-a", "task_id": "run"},
                   {"ts": t0 + 0.1, "event": "task_awaiting_input", "goal_id": "op-a", "task_id": "run",
                    "question": "选哪个？"}])
    c = _collect(proj)
    assert [x["kind"] for x in c["framework_candidates"]] == ["awaiting_reentry"]


def test_max_flows_caps_selection(proj):
    _seed_week(proj)
    c = _collect(proj, max_flows=1)
    assert c["selected"] == ["operate"]
    assert "超出单次上限" in c["flows"]["digest"]["reason"]


def test_shared_agent_owner_and_scope(proj):
    _seed_week(proj)
    _job(proj, "tb-1030", [("scan", "scout", [{"active_s": 100}]), ("news", "scout", [{"active_s": 500}]),
                           ("page", "builder", [{"active_s": 100}])], "2026-10-30", wall=700)
    c = _collect(proj)
    dg, tb = c["flows"]["digest"], c["flows"]["tabbed"]
    assert dg["owned_agents"] == ["builder", "scout"]               # digest Job 更多，归它
    assert tb["owned_agents"] == [] and tb["requests_agents"] == {"scout": "digest", "builder": "digest"}
    assert "tabbed" in dg["shared_agents"]["scout"]
    assert opt.allowed(dg, "agents/scout/prompt.md") and opt.allowed(dg, "agents/scout/workspace/CLAUDE.md")
    assert not opt.allowed(tb, "agents/scout/prompt.md")
    assert opt.allowed(tb, "coordinator/plans/tabbed.json")
    assert not opt.allowed(dg, "agents/scout/agent.yaml") and not opt.allowed(dg, "agent_team/monitor.py")


def test_collect_refuses_existing_campaign(proj):
    _seed_week(proj)
    _collect(proj)
    with pytest.raises(FileExistsError):
        _collect(proj)
    assert opt.new_campaign_id(proj, TODAY) == "opt-20261102"


# ---------------- 指标词表 ----------------

def test_metric_values_and_compare(proj):
    _seed_week(proj)
    m = _collect(proj)["flows"]["digest"]["metrics"]
    assert opt.metric_value(m, "task:build.span_s.p50") == 650.0
    assert opt.metric_value(m, "job.rejects") == 0.5
    assert opt.metric_value(m, "task:ghost.span_s.p50") is None
    assert opt.valid_metric("task:build.span_s.p50") and opt.valid_metric("job.success_rate")
    assert not opt.valid_metric("task:build.speed") and not opt.valid_metric("whatever")
    assert opt.compare("task:x.span_s.p50", 600, 400) == "improved"
    assert opt.compare("job.success_rate", 0.5, 1.0) == "improved"
    assert opt.compare("job.rejects", 1, 1.02) == "unchanged"
    assert opt.compare("job.rejects", 1, 2) == "regressed"
    assert opt.compare("job.rejects", None, 2) == "no_data"


def test_contract_metrics_count_events_and_fanout_aggregate(proj):
    # 契约类指标：task_artifacts_missing → artifact_warnings；task_contract_violation → contract_violations，
    # 单步违约按 _task_key 聚合（fan-out 子任务 detail.1/.2 归到模板 id detail）
    b = _job(proj, "fx-1", [("scan", "scout", [{"active_s": 100}]), ("detail.1", "scout", [{"active_s": 50}]),
                            ("detail.2", "scout", [{"active_s": 60}])], "2026-10-28", wall=400)
    for i in (1, 2):
        b.tasks[f"detail.{i}"].fanout_group = "detail"
    t0 = _ts("2026-10-28", 11)
    events = [(10, {"ts": t0, "event": "task_artifacts_missing", "goal_id": "fx-1", "task_id": "scan",
                    "missing": ["/a/x.json"]}),
              (11, {"ts": t0, "event": "task_contract_violation", "goal_id": "fx-1", "task_id": "detail.1",
                    "agent": "scout", "violations": ["x.json[0].thread 期望 array 实际 string"]}),
              (12, {"ts": t0, "event": "task_contract_violation", "goal_id": "fx-1", "task_id": "detail.2",
                    "agent": "scout", "violations": ["x.json 无匹配文件", "y.json 根类型错"]})]
    row = opt.job_metrics(b, events, now=t0 + 1000)
    assert row["counts"]["artifact_warnings"] == 1 and row["counts"]["contract_violations"] == 2
    by = {t["task_id"]: t for t in row["tasks"]}
    assert set(by) == {"scan", "detail"} and by["detail"]["contract_violations"] == 2
    assert by["scan"]["contract_violations"] == 0
    cv = [s for s in row["signals"] if s["kind"] == "contract_violation"]
    assert [s["evidence"] for s in cv] == ["run.log:11", "run.log:12"]
    assert cv[1]["detail"] == "x.json 无匹配文件；y.json 根类型错" and cv[1]["label"] == "产物契约违约"
    assert not cv[0]["framework"] and opt.SIGNAL_WEIGHTS["contract_violation"] > 0   # 流程类，计分
    assert [s["evidence"] for s in row["signals"] if s["kind"] == "artifact_warning"] == ["run.log:10"]
    m = opt.flow_metrics([row, opt.job_metrics(b, [], now=t0 + 1000)])       # 第二个 Job 无违约
    assert m["totals"]["contract_violations"] == 2 and m["totals"]["artifact_warnings"] == 1
    assert opt.metric_value(m, "job.contract_violations") == 1.0
    assert opt.metric_value(m, "job.artifact_warnings") == 0.5
    assert opt.metric_value(m, "task:detail.contract_violations") == 1.0
    assert opt.metric_value(m, "task:scan.contract_violations") == 0.0


def test_contract_metrics_in_vocabulary_and_scored_by_collect(proj):
    for k in ("job.contract_violations", "job.artifact_warnings", "task:build.contract_violations"):
        assert opt.valid_metric(k)
    assert "job.contract_violations" in opt.METRIC_HELP and "task:<task_id>.contract_violations" in opt.METRIC_HELP
    _job(proj, "qt-c", [("solo", "op", [{"active_s": 60}])], "2026-10-29", wall=100)
    # 贴近真实 run.log：monitor 每次违约都先记 task_contract_violation，紧跟一条 task_failed
    t0 = _ts("2026-10-29", 11)
    _events(proj, [{"ts": t0, "event": "task_contract_violation", "goal_id": "qt-c",
                    "task_id": "solo", "agent": "op", "violations": ["out.json 缺字段 id"]},
                   {"ts": t0, "event": "task_failed", "goal_id": "qt-c", "task_id": "solo",
                    "reason": "artifact contract violation：out.json 缺字段 id"}])
    c = _collect(proj)
    q = c["flows"]["quiet"]
    assert opt.SIGNAL_WEIGHTS["contract_violation"] == opt.SIGNAL_WEIGHTS["artifact_warning"] == 1
    assert q["status"] == "selected" and q["score"] == 4                     # failure 3 + contract_violation 1
    assert q["signal_counts"] == {"contract_violation": 1, "failure": 1}
    assert q["metrics"]["totals"]["contract_violations"] == 1 and q["metrics"]["totals"]["failures"] == 1
    ev = (opt.campaign_dir(proj, "opt-t") / "evidence" / "quiet.md").read_text(encoding="utf-8")
    assert "contract_violations=1" in ev and "[产物契约违约]" in ev and "out.json 缺字段 id" in ev


def test_contract_metrics_missing_in_legacy_metrics_yield_no_data(proj):
    # 早于新增计数键的旧 campaign 指标：per_job / tasks 行里没有 contract_violations，取值 None 而非 KeyError
    legacy = {"jobs": 2, "success_rate": 1.0, "wall_p50_s": 100.0, "wall_max_s": 120.0, "awaiting_s": 0.0,
              "totals": {"rejects": 1}, "per_job": {"rejects": 0.5},
              "tasks": [{"task_id": "build", "agent": "builder", "runs": 2, "failed_runs": 0, "jobs": 2,
                         "span_p50_s": 50.0, "span_max_s": 60.0}]}
    assert opt.metric_value(legacy, "job.rejects") == 0.5
    for k in ("job.contract_violations", "job.artifact_warnings", "task:build.contract_violations"):
        assert opt.metric_value(legacy, k) is None
    # 旧历史里的改动基线缺失（None），下次 collect 的前后对比得 no_data
    _seed_week(proj)
    _history(proj, {"campaign_id": "opt-old", "finished_at": _ts("2026-10-26", 0),
                    "flows": {"digest": {"status": "merged", "changes": [
                        {"title": "契约", "direction": "contract", "metric": "job.contract_violations",
                         "baseline": None}]}}})
    eff = _collect(proj)["previous_effect"]
    assert len(eff) == 1 and eff[0]["current"] == 0.0 and eff[0]["verdict"] == "no_data"


# ---------------- 日志与状态 ----------------

def test_log_advances_status_and_limits_fix_rounds(proj):
    _seed_week(proj)
    _collect(proj)
    opt.log(proj, "opt-t", "review_started", flow="digest")
    opt.log(proj, "opt-t", "fix_round", flow="digest")
    opt.log(proj, "opt-t", "fix_round", flow="digest")
    with pytest.raises(ValueError, match="abandoned"):
        opt.log(proj, "opt-t", "fix_round", flow="digest")
    c = opt.load_campaign(proj, "opt-t")
    assert c["flows"]["digest"]["status"] == "fixing" and c["flows"]["digest"]["fix_rounds"] == 2
    events = [e["event"] for e in opt.read_log(proj, "opt-t")]
    assert events[:2] == ["campaign_started", "evidence_collected"] and events.count("fix_round") == 2
    with pytest.raises(KeyError):
        opt.log(proj, "opt-t", "note", flow="nope")


# ---------------- worktree / 门禁 / 合并 ----------------

def _optimize_digest(proj, wt: Path, files: dict[str, str], subject: str = "optimize(digest): 前置字段要求") -> None:
    for rel, text in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", subject)


def _write_opt_report(proj, flow="digest", **over):
    d = opt.flow_dir(proj, "opt-t", flow)
    d.mkdir(parents=True, exist_ok=True)
    rep = {"summary": "把打回意见前置到 build 的 goal", "changes": [{
        "title": "build 交付前自检字段", "direction": "rework", "problem": "build 被打回缺字段",
        "evidence": ["run.log:1 task_rejected dg-1027/build 缺字段"], "change": "goal 增加字段自检",
        "files": ["agents/builder/prompt.md"], "expected": "打回归零", "metric": "job.rejects"}],
        "cross_flow": [], "not_done": [], "risks": [],
        "framework_issues": [{"title": "中断提示被当成提问", "target": "agent_team/pane_scan.py",
                              "problem": "p", "proposal": "q", "evidence": ["run.log:3"],
                              "impact": "medium", "effort": "low", "confidence": 0.8}], **over}
    (d / "optimize.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")


def _write_review(proj, n: int, verdict: str, findings: list[dict] | None = None, flow="digest"):
    d = opt.flow_dir(proj, "opt-t", flow)
    (d / f"review-r{n}.json").write_text(json.dumps(
        {"verdict": verdict, "summary": "s", "findings": findings or []}, ensure_ascii=False), encoding="utf-8")


def test_gate_passes_within_scope(proj):
    _seed_week(proj)
    _collect(proj)
    info = ops.create_worktree(proj, "opt-t", "digest")
    wt = Path(info["worktree"])
    assert info["branch"] == "opt/opt-t/digest" and wt.exists()
    _optimize_digest(proj, wt, {"agents/builder/prompt.md": "# builder\n交付前自检字段\n"})
    _write_opt_report(proj)
    opt.record_optimizer(proj, "opt-t", "digest")
    res = ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    assert res["ok"], [c for c in res["checks"] if not c["ok"]]
    assert res["round"] == 1 and Path(res["file"]).name == "gate-r1.json"
    f = opt.load_campaign(proj, "opt-t")["flows"]["digest"]
    assert f["status"] == "gated" and f["commits"] == [res["head"]]


def test_gate_accepts_explicit_noop_without_changes(proj):
    # 优化者判定「本流程已无可改进项」：显式 noop + 空 changes + summary 说明，门禁不该判死
    _seed_week(proj)
    _collect(proj)
    ops.create_worktree(proj, "opt-t", "digest")
    _write_opt_report(proj, noop=True, changes=[],
                      summary="plan 层已无可改进项：热点已由 f5753ca 修复，窗口内无新证据")
    res = ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    assert res["ok"], [c for c in res["checks"] if not c["ok"]]
    assert res["noop"] is True
    assert opt.load_campaign(proj, "opt-t")["flows"]["digest"]["status"] == "gated"


def test_gate_still_rejects_empty_changes_without_noop(proj):
    _seed_week(proj)
    _collect(proj)
    ops.create_worktree(proj, "opt-t", "digest")
    _write_opt_report(proj, changes=[])          # 漏写 noop：不能和「无可改进项」混为一谈
    res = ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    bad = {c["name"]: c for c in res["checks"] if not c["ok"]}
    assert {"has_changes", "optimize_report"} <= set(bad)
    assert any("noop" in p for p in bad["optimize_report"]["detail"])


def test_noop_delivery_requires_summary(tmp_path):
    p = tmp_path / "optimize.json"
    p.write_text(json.dumps({"noop": True, "changes": [], "summary": "  "}), encoding="utf-8")
    assert any("summary" in x for x in ops.optimize_report_problems(p))
    p.write_text(json.dumps({"status": "noop", "changes": [], "summary": "没问题"}), encoding="utf-8")
    assert ops.optimize_report_problems(p) == []


def test_gate_rejects_out_of_scope_prefix_and_bad_report(proj):
    _seed_week(proj)
    _collect(proj)
    wt = Path(ops.create_worktree(proj, "opt-t", "digest")["worktree"])
    _optimize_digest(proj, wt, {"agent_team/x.py": "x = 1\n", "agents/op/prompt.md": "改了别人的\n"},
                     subject="tweak stuff")
    _write_opt_report(proj, changes=[{"title": "t", "direction": "speed", "files": [], "metric": "foo"}])
    res = ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    bad = {c["name"]: c for c in res["checks"] if not c["ok"]}
    assert set(bad) == {"scope", "commit_prefix", "optimize_report"}
    assert set(bad["scope"]["detail"]["out_of_scope"]) == {"agent_team/x.py", "agents/op/prompt.md"}
    assert any("direction" in p for p in bad["optimize_report"]["detail"])
    assert any("验证指标" in p for p in bad["optimize_report"]["detail"])
    assert opt.load_campaign(proj, "opt-t")["flows"]["digest"]["status"] == "gate_failed"


def test_gate_catches_broken_plan_and_dirty_tree(proj):
    _seed_week(proj)
    _collect(proj)
    wt = Path(ops.create_worktree(proj, "opt-t", "digest")["worktree"])
    plan = json.loads((wt / "coordinator/plans/digest.json").read_text(encoding="utf-8"))
    plan["tasks"][1]["needs"] = ["ghost"]
    plan["tasks"][0]["goal"] = "扫 {{q}} {{extra}}"
    _optimize_digest(proj, wt, {"coordinator/plans/digest.json": json.dumps(plan, ensure_ascii=False)})
    (wt / "agents/builder/prompt.md").write_text("未提交\n", encoding="utf-8")
    _write_opt_report(proj)
    res = ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    bad = {c["name"]: c for c in res["checks"] if not c["ok"]}
    assert {"clean", "plans"} <= set(bad)
    assert any("ghost" in p for p in bad["plans"]["detail"])


def test_review_pass_with_open_blocking_is_refused(proj):
    _seed_week(proj)
    _collect(proj)
    ops.create_worktree(proj, "opt-t", "digest")
    _write_review(proj, 1, "PASS", [{"severity": "blocking", "title": "x", "status": "open"}])
    with pytest.raises(ValueError, match="blocking"):
        opt.record_review(proj, "opt-t", "digest")
    _write_review(proj, 1, "FAIL", [{"severity": "blocking", "title": "x", "status": "open"}])
    assert opt.record_review(proj, "opt-t", "digest")["verdict"] == "FAIL"
    assert opt.load_campaign(proj, "opt-t")["flows"]["digest"]["status"] == "changes_requested"


def test_merge_only_approved_and_gated_then_cleanup(proj):
    _seed_week(proj)
    _collect(proj)
    for flow, rel in (("digest", "agents/builder/prompt.md"), ("operate", "agents/op/prompt.md")):
        wt = Path(ops.create_worktree(proj, "opt-t", flow)["worktree"])
        _optimize_digest(proj, wt, {rel: f"# {flow}\n改进\n"}, subject=f"optimize({flow}): 改进")
        _write_opt_report(proj, flow=flow, changes=[{
            "title": "t", "direction": "reliability", "evidence": ["e"], "files": [rel],
            "metric": "job.wall_s.p50"}])
        opt.record_optimizer(proj, "opt-t", flow)
        assert ops.gate_flow(proj, "opt-t", flow, skip_slow=True)["ok"]
    _write_review(proj, 1, "PASS")
    opt.record_review(proj, "opt-t", "digest")
    _write_review(proj, 1, "FAIL", [{"severity": "blocking", "title": "x"}], flow="operate")
    opt.record_review(proj, "opt-t", "operate")
    res = ops.merge(proj, "opt-t", skip_slow=True)
    assert res["merged"] == ["digest"] and res["conflict"] is None and res["main_gate"]["ok"]
    assert "改进" in (proj.root / "agents/builder/prompt.md").read_text(encoding="utf-8")
    assert "改进" not in (proj.root / "agents/op/prompt.md").read_text(encoding="utf-8")
    assert "merge: 合入 digest" in _git(proj.root, "log", "-1", "--format=%s")
    c = opt.load_campaign(proj, "opt-t")
    assert c["flows"]["digest"]["status"] == "merged" and c["main_gate"]["ok"]
    assert not (proj.root / ".worktrees/opt-t/digest").exists()
    branches = _git(proj.root, "branch", "--format=%(refname:short)")
    assert "opt/opt-t/digest" not in branches and "opt/opt-t/operate" in branches   # 未合并分支保留备查


def test_merge_refuses_dirty_main(proj):
    _seed_week(proj)
    _collect(proj)
    (proj.root / "agents/op/prompt.md").write_text("脏\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="未提交"):
        ops.merge(proj, "opt-t", skip_slow=True)


# ---------------- 框架问题 / 历史 / 报告 ----------------

def test_framework_issues_recorded_with_dedup(proj):
    _seed_week(proj)
    _collect(proj)
    ops.create_worktree(proj, "opt-t", "digest")
    _write_opt_report(proj)
    _write_review(proj, 1, "PASS")
    (opt.flow_dir(proj, "opt-t", "digest") / "review-r1.json").write_text(json.dumps({
        "verdict": "PASS", "findings": [], "framework_issues": [{
            "title": "中断提示被当成提问", "target": "agent_team/pane_scan.py", "evidence": ["评审补充证据"]}]},
        ensure_ascii=False), encoding="utf-8")
    issues = opt.gather_framework_issues(proj, "opt-t", [{"title": "provision 重试慢", "target": "dispatch.py"}])
    assert len(issues) == 3
    st = opt.record_framework_issues(proj, "opt-t", issues)
    assert st["new"] == 2 and st["recurred"] == 0          # 同一指纹在同一 campaign 只记一次来源
    items = retro._load_backlog(proj)["items"]
    assert {it["category"] for it in items} == {"framework"} and {it["scope"] for it in items} == {"project"}
    hit = next(it for it in items if it["title"] == "中断提示被当成提问")
    assert "评审补充证据" in hit["evidence"] and hit["sources"] == ["opt-t"]
    # 下一次 campaign 再报：复现计数 +1（opt-t 没收尾，需 force 才能并行新建）
    opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-t2", force=True)
    opt.record_framework_issues(proj, "opt-t2", [{"title": "中断提示被当成提问", "target": "agent_team/pane_scan.py"}])
    hit = next(it for it in retro._load_backlog(proj)["items"] if it["title"] == "中断提示被当成提问")
    assert hit["occurrences"] == 2


def test_finish_writes_result_history_and_report(proj):
    _seed_week(proj)
    _collect(proj)
    wt = Path(ops.create_worktree(proj, "opt-t", "digest")["worktree"])
    _optimize_digest(proj, wt, {"agents/builder/prompt.md": "# builder\n<b>自检</b>\n"})
    _write_opt_report(proj, summary="把 <script>alert(1)</script> 前置")
    opt.record_optimizer(proj, "opt-t", "digest")
    ops.gate_flow(proj, "opt-t", "digest", skip_slow=True)
    _write_review(proj, 1, "FAIL", [{"severity": "major", "title": "措辞冲突", "location": "prompt.md:2"}])
    opt.record_review(proj, "opt-t", "digest")
    opt.log(proj, "opt-t", "fix_round", flow="digest")
    _write_review(proj, 2, "PASS", [{"severity": "major", "title": "措辞冲突", "status": "fixed"}])
    opt.record_review(proj, "opt-t", "digest")
    ops.merge(proj, "opt-t", skip_slow=True)
    opt.record_framework_issues(proj, "opt-t", opt.gather_framework_issues(proj, "opt-t"))
    res = opt.finish(proj, "opt-t")
    d = opt.campaign_dir(proj, "opt-t")
    assert json.loads((d / "result.json").read_text(encoding="utf-8"))["totals"]["merged"] == 1
    dg = next(f for f in res["flows"] if f["name"] == "digest")
    assert dg["changes"][0]["baseline"] == 0.5               # 基线按证据现算，不用自报
    assert dg["diffstat"]["files"][0]["path"] == "agents/builder/prompt.md"
    assert [r["verdict"] for r in dg["reviews"]] == ["FAIL", "PASS"]
    # 复审带上一轮发现并更新状态：同标题合并成一条，保留首次发现轮次与最新状态
    assert [(x["round"], x["last_round"], x["status"]) for x in dg["findings"]] == [(1, 2, "fixed")]
    hist = json.loads(opt.history_file(proj).read_text(encoding="utf-8"))
    assert hist["campaigns"][0]["flows"]["digest"]["changes"][0]["metric"] == "job.rejects"
    assert "quiet" not in hist["campaigns"][0]["flows"]
    page = (d / "report.html").read_text(encoding="utf-8")
    for part in ("流程优化报告", "流程健康", "评审台账", "框架问题", "上次优化效果", "时间线", "<svg"):
        assert part in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    opt.finish(proj, "opt-t")                                # 可重复执行：历史按 campaign 覆盖
    assert len(json.loads(opt.history_file(proj).read_text(encoding="utf-8"))["campaigns"]) == 1


def test_previous_effect_uses_only_jobs_after_last_campaign(proj):
    _seed_week(proj)
    opt.history_file(proj).write_text(json.dumps({"version": 1, "campaigns": [{
        "campaign_id": "opt-old", "finished_at": _ts("2026-10-27", 9),
        "flows": {"digest": {"status": "merged", "changes": [
            {"title": "build 提速", "direction": "latency", "metric": "task:build.span_s.p50", "baseline": 800}]},
                  "operate": {"status": "abandoned", "changes": [{"title": "x", "metric": "job.timeouts"}]}}}]}),
        encoding="utf-8")
    eff = _collect(proj)["previous_effect"]
    assert len(eff) == 1                                    # 放弃的流程不评估
    assert eff[0]["jobs_after"] == 1 and eff[0]["current"] == 700.0 and eff[0]["verdict"] == "improved"


def test_signals_link_previous_change_and_flag_unverified(proj):
    _seed_week(proj)
    opt.history_file(proj).write_text(json.dumps({"version": 1, "campaigns": [
        {"campaign_id": "opt-old", "finished_at": _ts("2026-10-25", 9),
         "flows": {"digest": {"status": "merged", "changes": [
             {"title": "旧改动", "metric": "task:build.span_s.p50", "baseline": 800}]}}},
        {"campaign_id": "opt-new", "finished_at": _ts("2026-10-26", 15),
         "flows": {"digest": {"status": "merged", "changes": [
             {"title": "翻译前移", "metric": "task:build.span_s.p50", "baseline": 800},
             {"title": "scan 提速", "metric": "task:scan.span_s.p50", "baseline": 200}]},
                   "operate": {"status": "abandoned", "changes": [
             {"title": "run 改造", "metric": "task:run.span_s.p50", "baseline": 500}]}}}]}),
        encoding="utf-8")
    c = _collect(proj)
    d = opt.campaign_dir(proj, "opt-t") / "evidence"
    fj = json.loads((d / "flows.json").read_text(encoding="utf-8"))
    sigs = [s for r in fj["jobs"]["digest"] for s in r["signals"] if s["task_id"] == "build"]
    assert sigs and all(len(s["previous_changes"]) == 1 for s in sigs)   # 每个信号最多关联一条
    assert {s["previous_changes"][0]["title"] for s in sigs} == {"翻译前移"}   # 取最近一次合并，旧改动不参与
    assert set(sigs[0]["previous_changes"][0]) == {"title", "metric", "current", "jobs_after", "verdict"}
    ev = (d / "digest.md").read_text(encoding="utf-8")
    assert "相关上次改动：翻译前移（task:build.span_s.p50）" in ev
    assert "改动后该流程共 1 个 Job" in ev
    # 无匹配信号保持原样；abandoned 流程的改动不参与关联
    assert "相关上次改动" not in (d / "operate.md").read_text(encoding="utf-8")
    assert all(not s["previous_changes"] for r in fj["jobs"]["operate"] for s in r["signals"])
    # 关联只做提示；dg-1026 早于 opt-new 合并，它的热点不计分，只剩 dg-1027 的打回 2 + 热点 1
    assert c["flows"]["digest"]["signal_counts"]["hotspot"] == 2
    assert c["flows"]["digest"]["stale_jobs"] == ["dg-1026"]
    assert c["flows"]["digest"]["score"] == 3


def test_signal_change_with_jobs_after_shows_verdict(proj):
    _seed_week(proj)
    opt.history_file(proj).write_text(json.dumps({"version": 1, "campaigns": [{
        "campaign_id": "opt-old", "finished_at": _ts("2026-10-26", 0),
        "flows": {"digest": {"status": "merged", "changes": [
            {"title": "build 提速", "metric": "task:build.span_s.p50", "baseline": 800}]}}}]}),
        encoding="utf-8")
    _collect(proj)
    ev = (opt.campaign_dir(proj, "opt-t") / "evidence" / "digest.md").read_text(encoding="utf-8")
    assert "相关上次改动：build 提速（task:build.span_s.p50）" in ev
    assert "当前 650，improved（改动后该流程共 2 个 Job）" in ev
    assert "效果未验证" not in ev


def test_previous_changes_skips_non_task_metrics():
    def e(metric: str) -> dict:
        return {"flow": "digest", "metric": metric, "title": "t", "current": None, "jobs_after": 0,
                "verdict": "no_data"}

    assert opt._metric_task("job.rejects") == ""          # job.* 不是单步指标
    assert opt._metric_task("task:build") == ""           # 没有指标后缀，无法关联
    assert opt._metric_task("task:.span_s.p50") == ""     # 空 task id
    pc = opt.previous_changes([e("job.rejects"), e("task:build"), e("task:.span_s.p50"),
                               e("task:build.span_s.p50")])
    assert set(pc) == {("digest", "build")}
    assert pc[("digest", "build")]["metric"] == "task:build.span_s.p50"


# ---------------- 书签 ----------------

def _history(proj, *campaigns: dict) -> None:
    opt.history_file(proj).write_text(json.dumps({"version": 1, "campaigns": list(campaigns)}), encoding="utf-8")


def test_default_window_resumes_from_bookmark(proj):
    assert opt.default_window(proj, today=TODAY)["label"] == "上一个自然周"     # 没有历史
    _history(proj, {"campaign_id": "a", "finished_at": 1, "window": {"start": "2026-10-19", "end": "2026-10-25"}},
             {"campaign_id": "b", "finished_at": 2, "window": {"start": "2026-10-12", "end": "2026-10-18"}},
             {"campaign_id": "c", "window": {"start": "2026-10-26", "end": "2026-11-01"}})   # 未收尾不算覆盖
    w = opt.default_window(proj, today=TODAY)
    assert (w["start"], w["end"]) == ("2026-10-26", "2026-11-01") and "2026-10-25" in w["label"]
    w = opt.default_window(proj, today=date(2026, 11, 20))                     # 隔几周没跑也不漏
    assert (w["start"], w["end"]) == ("2026-10-26", "2026-11-19")
    with pytest.raises(ValueError, match="书签已覆盖到"):
        opt.default_window(proj, today=date(2026, 10, 26))


def test_collect_refuses_while_campaign_unfinished_until_abandoned(proj):
    _seed_week(proj)
    _collect(proj)
    with pytest.raises(RuntimeError, match="opt-t"):
        opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-t2")
    opt.abandon_campaign(proj, "opt-t", reason="重开")
    assert opt.unfinished_campaigns(proj) == [] and opt.load_campaign(proj, "opt-t")["abandon_reason"] == "重开"
    opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-t2")
    opt.finish(proj, "opt-t2")
    with pytest.raises(ValueError, match="已 finish"):
        opt.abandon_campaign(proj, "opt-t2")
    assert opt.latest_campaign(proj) == "opt-t2"


def test_abandon_refuses_campaign_with_merged_flow(proj):
    _seed_week(proj)
    _collect(proj)
    c = opt.load_campaign(proj, "opt-t")
    c["flows"]["digest"]["status"] = "merged"
    opt.save_campaign(proj, c)
    with pytest.raises(ValueError, match="finish"):
        opt.abandon_campaign(proj, "opt-t")


def test_flow_with_only_pre_change_jobs_is_not_selected(proj):
    _seed_week(proj)
    _history(proj, {"campaign_id": "opt-old", "finished_at": _ts("2026-10-29", 9),
                    "window": {"start": "2026-10-19", "end": "2026-10-25"},
                    "flows": {"operate": {"status": "merged", "changes": []}}})
    c = _collect(proj)
    op = c["flows"]["operate"]
    assert op["status"] == "skipped" and op["score"] == 0 and op["stale_jobs"] == ["op-1028"]
    assert "早于上次优化 opt-old" in op["reason"] and c["selected"] == ["digest"]
    assert op["metrics"]["jobs"] == 1                                         # 只供报告，仍可见


def test_bookmark_lists_flows_and_new_jobs(proj):
    _seed_week(proj)
    _history(proj, {"campaign_id": "opt-a", "finished_at": _ts("2026-10-27", 9),
                    "window": {"start": "2026-10-19", "end": "2026-10-25"},
                    "flows": {"digest": {"status": "merged", "changes": [{"title": "前置字段"}]},
                              "operate": {"status": "abandoned", "changes": []}}})
    bm = opt.bookmark(proj, today=TODAY)
    assert bm["covered_until"] == "2026-10-25" and bm["next_window"]["start"] == "2026-10-26"
    rows = {r["flow"]: r for r in bm["flows"]}
    assert set(rows) == {"digest", "tabbed", "operate", "quiet"}
    assert rows["digest"]["last_merged"]["changes"] == ["前置字段"]
    assert rows["digest"]["new_jobs"] == 2                       # dg-1027 与 11-02 的 dg-1102
    assert rows["operate"]["last_merged"] is None and rows["operate"]["last_attempt"]["status"] == "abandoned"
    assert rows["tabbed"]["last_attempt"] is None and rows["quiet"]["new_jobs"] == 1


def test_report_renders_empty_campaign(proj):
    _collect(proj)
    page = optimize_report.render(opt.build_result(proj, "opt-t"))
    assert "本次没有入选优化的流程" in page and "没有可对比的历史优化" in page


def test_render_prompt_fills_all_slots(proj):
    _seed_week(proj)
    _collect(proj)
    ops.create_worktree(proj, "opt-t", "digest")
    tdir = opt.templates_dir(proj)
    tdir.mkdir(parents=True)
    (tdir / "optimizer.md").write_text("# 给编排者\n\n{{flow}} {{worktree}} {{campaign}}\n{{allowed_list}}\n"
                                       "{{shared_list}}\n{{requests_list}}\n{{jobs}} {{evidence_file}}", encoding="utf-8")
    (tdir / "reviewer.md").write_text("# t\n\nr{{round}} {{head}} {{prev_head}}", encoding="utf-8")
    text = opt.render_prompt(proj, "opt-t", "digest", "optimizer")
    assert "{{" not in text and "给编排者" not in text
    assert "`agents/builder/prompt.md`" in text and "dg-1026、dg-1027" in text
    assert opt.render_prompt(proj, "opt-t", "digest", "reviewer").startswith("r1 ")
    _write_review(proj, 1, "FAIL")
    assert opt.render_prompt(proj, "opt-t", "digest", "reviewer").startswith("r2 ")
    with pytest.raises(ValueError):
        opt.render_prompt(proj, "opt-t", "operate", "optimizer")      # 还没 worktree


def test_real_skill_templates_have_no_unknown_slots():
    """仓库里真实的 skill 模板只能用 render_prompt 认识的槽，否则派出去会残留 {{…}}。"""
    root = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "team-optimize" / "templates"
    known = {"flow", "campaign", "window", "worktree", "branch", "base_commit", "repo_root", "flow_dir", "head",
             "prev_head", "round", "evidence_file", "jobs", "allowed_list", "shared_list", "requests_list"}
    for name in ("optimizer.md", "reviewer.md"):
        body = (root / name).read_text(encoding="utf-8").split("\n", 1)[1]
        slots = set(re.findall(r"\{\{(\w+)\}\}", body)) - {"槽", "shard", "total"}
        assert slots <= known, (name, slots - known)


# ---------------- CLI ----------------

def test_cli_flow(proj, capsys):
    _seed_week(proj)
    assert cli.main(["optimize", "collect", "--id", "opt-t", "--since", "2026-10-26", "--until", "2026-11-01"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["selected"] == ["operate", "digest"]
    assert cli.main(["optimize", "worktree", "digest"]) == 0          # 默认取最近一次 campaign
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["optimize", "scope", "digest", "agent_team/x.py"])
    assert cli.main(["optimize", "scope", "digest", "agents/op/workspace/notes.md", "--reason", "共用经验"]) == 0
    assert opt.allowed(opt.load_campaign(proj, "opt-t")["flows"]["digest"], "agents/op/workspace/notes.md")
    assert cli.main(["optimize", "log", "note", "--flow", "digest", "--field", "msg=hi"]) == 0
    assert cli.main(["optimize", "check", "digest", "--skip-slow"]) == 1   # 还没改动
    with pytest.raises(SystemExit):
        cli.main(["optimize", "worktree", "quiet"])                  # 健康流程不能拉 worktree
    assert cli.main(["optimize", "finish"]) == 0
    assert (opt.campaign_dir(proj, "opt-t") / "report.html").exists()
    capsys.readouterr()
    assert cli.main(["optimize", "bookmark"]) == 0
    assert json.loads(capsys.readouterr().out)["covered_until"] == "2026-11-01"


# ---------------- 跨流程请求 ----------------

def test_normalize_cross_flow_upgrades_legacy_strings():
    reqs = {"html-gen": "tabbed", "x-scout": "tabbed"}
    out = opt.normalize_cross_flow(["html-gen（owner: tabbed）：build 慢，请收敛断点", "  ", "不点名任何 agent"],
                                   "digest", reqs)
    assert [x["request"] for x in out] == ["html-gen（owner: tabbed）：build 慢，请收敛断点", "不点名任何 agent"]
    assert [x["owner"] for x in out] == ["tabbed", ""]
    assert {x["status"] for x in out} == {"open"} and all(x["requester"] == "digest" for x in out)
    assert out[0]["id"].startswith("xf-") and out[0]["id"] != out[1]["id"]
    # 指纹稳定：空白差异不影响 id；发起流程不同则 id 不同
    again = opt.normalize_cross_flow(["html-gen（owner: tabbed）：build  慢，请收敛断点"], "digest", reqs)
    assert again[0]["id"] == out[0]["id"]
    assert opt.normalize_cross_flow([out[0]["request"]], "other", reqs)[0]["id"] != out[0]["id"]
    assert opt.normalize_cross_flow(None, "digest") == [] and opt.normalize_cross_flow("x", "digest") == []


def test_normalize_cross_flow_keeps_objects_and_drops_invalid():
    out = opt.normalize_cross_flow([
        {"owner": "tabbed", "target_flow": "digest", "request": "同步边界", "evidence": ["run.log:3"],
         "status": "DONE", "verification": "prompt:30", "extra": "丢掉"},
        {"request": "未知状态", "status": "wip", "evidence": "单条证据"},
        {"id": "xf-old", "requester": "digest", "owner": "tabbed", "request": "沿用 id", "status": "deferred"},
        {"owner": "tabbed"}, 42, None, ["x"]], "tabbed")
    assert len(out) == 3
    assert set(out[0]) == {"id", "requester", "owner", "target_flow", "request", "evidence", "status", "verification"}
    assert (out[0]["status"], out[0]["verification"], out[0]["evidence"]) == ("done", "prompt:30", ["run.log:3"])
    assert (out[1]["status"], out[1]["evidence"]) == ("open", ["单条证据"])
    assert (out[2]["id"], out[2]["requester"]) == ("xf-old", "digest")       # 闭环时沿用上一轮的 id 与发起方


def test_gate_light_checks_cross_flow(tmp_path):
    p = tmp_path / "optimize.json"
    base = {"summary": "s", "changes": [{"title": "t", "direction": "rework", "evidence": ["e"], "files": ["f"],
                                         "metric": "job.rejects"}]}

    def probs(cf):
        p.write_text(json.dumps({**base, "cross_flow": cf}, ensure_ascii=False), encoding="utf-8")
        return ops.optimize_report_problems(p)

    assert probs(["旧格式自由文本也照收"]) == []
    assert probs([{"request": "r", "status": "done", "verification": "v"}, {"request": "r2"}]) == []
    bad = probs([{"owner": "x"}, {"request": "r", "status": "wip"}, 3])
    assert any("cross_flow[0] 缺 request" in x for x in bad)
    assert any("cross_flow[1] status='wip'" in x for x in bad)
    assert any("cross_flow[2] 必须是对象" in x for x in bad)
    assert any("必须是数组" in x for x in probs("一句话"))
    p.write_text(json.dumps({"noop": True, "changes": [], "summary": "无", "cross_flow": [{"status": "x"}]}),
                 encoding="utf-8")
    assert len(ops.optimize_report_problems(p)) == 2                         # noop 交付也校验 cross_flow


def _finish_digest_with(proj, cid: str, cross_flow: list, status: str = "merged") -> dict:
    """直接造一个已收尾 campaign：digest 流程带指定 cross_flow，按需标成 merged/abandoned。"""
    opt.collect(proj, opt.resolve_window(today=TODAY), cid=cid, force=True)
    c = opt.load_campaign(proj, cid)
    c["flows"]["digest"]["status"] = status
    opt.save_campaign(proj, c)
    d = opt.flow_dir(proj, cid, "digest")
    d.mkdir(parents=True, exist_ok=True)
    (d / "optimize.json").write_text(json.dumps({"summary": "s", "changes": [], "cross_flow": cross_flow},
                                                ensure_ascii=False), encoding="utf-8")
    return opt.finish(proj, cid)


def test_pending_cross_flow_flows_into_next_collect_until_done(proj):
    _seed_week(proj)
    res = _finish_digest_with(proj, "opt-a", [
        {"owner": "operate", "request": "op 输出补 run_id 字段", "evidence": ["run.log:2"]},
        {"owner": "operate", "request": "已顺手处理", "status": "done", "verification": "v"}])
    hist = json.loads(opt.history_file(proj).read_text(encoding="utf-8"))
    saved = hist["campaigns"][-1]["flows"]["digest"]["cross_flow"]
    assert [x["status"] for x in saved] == ["open", "done"] and saved[0]["id"].startswith("xf-")
    assert res["totals"]["cross_flow_open"] == 1
    xid = saved[0]["id"]
    c = opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-b", force=True)
    assert [(x["id"], x["requester"], x["owner"], x["campaign_id"]) for x in c["pending_cross_flow"]] == \
        [(xid, "digest", "operate", "opt-a")]
    ev = (opt.campaign_dir(proj, "opt-b") / "evidence" / "operate.md").read_text(encoding="utf-8")
    assert "## 上次未闭环的跨流程请求" in ev and xid in ev and "op 输出补 run_id 字段" in ev
    # owner 在未合并的流程里标 done 不算闭环
    opt.abandon_campaign(proj, "opt-b")
    cid = "opt-c"
    opt.collect(proj, opt.resolve_window(today=TODAY), cid=cid, force=True)
    c = opt.load_campaign(proj, cid)
    c["flows"]["operate"]["status"] = "abandoned"
    opt.save_campaign(proj, c)
    d = opt.flow_dir(proj, cid, "operate")
    d.mkdir(parents=True, exist_ok=True)
    done = {"id": xid, "requester": "digest", "owner": "operate", "request": "op 输出补 run_id 字段",
            "status": "done", "verification": "op prompt 已加字段，见 commit abc"}
    (d / "optimize.json").write_text(json.dumps({"summary": "s", "changes": [], "cross_flow": [done]},
                                                ensure_ascii=False), encoding="utf-8")
    opt.finish(proj, cid)
    assert [x["id"] for x in opt.pending_cross_flow(proj)] == [xid]
    # owner 合并后同 id 标 done：闭环，下一次 collect 不再出现
    c = json.loads(opt.history_file(proj).read_text(encoding="utf-8"))
    c["campaigns"][-1]["flows"]["operate"]["status"] = "merged"
    opt.history_file(proj).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    assert opt.pending_cross_flow(proj) == []
    assert opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-d", force=True)["pending_cross_flow"] == []


def test_report_lists_carried_pending_cross_flow(proj):
    _seed_week(proj)
    _finish_digest_with(proj, "opt-a", [{"owner": "operate", "request": "往期请求甲"},
                                        {"owner": "operate", "request": "往期请求乙"}])
    xid = opt.pending_cross_flow(proj)[0]["id"]
    opt.collect(proj, opt.resolve_window(today=TODAY), cid="opt-b", force=True)
    _flow_opt(proj, "opt-b", "operate", [{"id": xid, "requester": "digest", "owner": "operate",
                                          "request": "往期请求甲", "status": "done", "verification": "v"}])
    _set_status(proj, "opt-b", operate="merged")
    res = opt.finish(proj, "opt-b")
    page = (opt.campaign_dir(proj, "opt-b") / "report.html").read_text(encoding="utf-8")
    assert "往期遗留（collect 时）" in page and "往期请求乙" in page
    assert "本次已闭环（operate）" in page and "仍未闭环，下次提示" in page
    assert res["totals"]["cross_flow_open"] == 0                               # KPI 仍只算本次
    assert [x["request"] for x in opt.pending_cross_flow(proj)] == ["往期请求乙"]
    # campaign.json 没有 pending_cross_flow（旧数据）时不出往期小节
    assert "往期遗留" not in optimize_report.cross_flow_section({**res, "pending_cross_flow": []})


def test_report_renders_cross_flow_table_and_legacy_strings(proj):
    _seed_week(proj)
    res = _finish_digest_with(proj, "opt-a", [
        "html-gen：旧格式 <b>自由文本</b>",
        {"owner": "operate", "target_flow": "operate", "request": "补字段", "status": "deferred",
         "verification": "下周再看", "evidence": ["run.log:1"]},
        {"owner": "operate", "request": "已做", "status": "done", "verification": "prompt:3"}])
    page = (opt.campaign_dir(proj, "opt-a") / "report.html").read_text(encoding="utf-8")
    assert "<h2>跨流程请求</h2>" in page and "未闭环跨流程请求" in page
    assert "按 id 去重：未闭环 2 条" in page and "本次已闭环 1 条" in page and "不跟踪 0 条" in page
    assert res["totals"]["cross_flow_open"] == 2 == len(opt.pending_cross_flow(proj))
    assert "&lt;b&gt;自由文本&lt;/b&gt;" in page and "下周再看" in page
    # 旧 result.json（cross_flow 是字符串、totals 没有 cross_flow_open）照常渲染，字符串现场归一化
    for f in res["flows"]:
        if f["name"] == "digest":
            f["cross_flow"] = ["旧字符串请求一", "旧字符串请求二"]
    res["totals"].pop("cross_flow_open")
    page = optimize_report.render(res)
    assert "旧字符串请求一" in page and "未闭环 2 条" in page
    assert optimize_report._open_cross_flow(res) == 2


def _flow_opt(proj, cid: str, flow: str, cross_flow: list) -> None:
    d = opt.flow_dir(proj, cid, flow)
    d.mkdir(parents=True, exist_ok=True)
    (d / "optimize.json").write_text(json.dumps({"summary": "s", "changes": [], "cross_flow": cross_flow},
                                                ensure_ascii=False), encoding="utf-8")


def _set_status(proj, cid: str, **statuses: str) -> None:
    c = opt.load_campaign(proj, cid)
    for flow, st in statuses.items():
        c["flows"][flow]["status"] = st
    opt.save_campaign(proj, c)


def test_same_campaign_closure_by_copying_fields_or_echoed_id(proj):
    _seed_week(proj)
    _collect(proj)
    req = {"owner": "operate", "request": "op 输出补 run_id 字段", "evidence": ["run.log:2"]}
    _flow_opt(proj, "opt-t", "digest", [req, {"owner": "operate", "request": "第二条"}])
    # 编排者从 record optimizer 的回显里拿 id
    ops.create_worktree(proj, "opt-t", "digest")
    echo = opt.record_optimizer(proj, "opt-t", "digest")["cross_flow"]
    assert [(x["owner"], x["status"]) for x in echo] == [("operate", "open")] * 2
    # owner 一条照抄 requester/owner/request 原文（不写 id），另一条写回显的 id
    _flow_opt(proj, "opt-t", "operate", [
        {"requester": "digest", "owner": "operate", "request": "op 输出补 run_id 字段", "status": "done",
         "verification": "op prompt 已加字段"},
        {"id": echo[1]["id"], "requester": "digest", "owner": "operate", "request": "第二条", "status": "Rejected",
         "verification": "不在 op 职责内"}])
    _set_status(proj, "opt-t", digest="merged", operate="merged")
    res = opt.finish(proj, "opt-t")
    op = next(f for f in res["flows"] if f["name"] == "operate")
    assert [x["id"] for x in op["cross_flow"]] == [x["id"] for x in echo]       # 两种写法都得到同一 id
    assert res["totals"]["cross_flow_open"] == 0 and opt.pending_cross_flow(proj) == []
    page = (opt.campaign_dir(proj, "opt-t") / "report.html").read_text(encoding="utf-8")
    assert "未闭环 0 条" in page and "本次已闭环 2 条" in page and "已闭环（operate）" in page


def test_unmerged_flow_requests_are_marked_untracked(proj):
    _seed_week(proj)
    _collect(proj)
    _flow_opt(proj, "opt-t", "digest", [{"owner": "operate", "request": "补字段"}])
    _set_status(proj, "opt-t", digest="abandoned")
    res = opt.finish(proj, "opt-t")
    assert res["totals"]["cross_flow_open"] == 0 and opt.pending_cross_flow(proj) == []
    page = (opt.campaign_dir(proj, "opt-t") / "report.html").read_text(encoding="utf-8")
    assert "未合并，不跟踪" in page and "未闭环 0 条" in page and "不跟踪 1 条" in page


def test_infer_owner_ascii_boundary_and_status_case():
    reqs = {"html-gen": "tabbed", "gen": "other"}
    assert opt.normalize_cross_flow(["请html-gen收敛断点"], "digest", reqs)[0]["owner"] == "tabbed"
    assert opt.normalize_cross_flow(["html-genx 不是它"], "digest", reqs)[0]["owner"] == ""
    assert ops.cross_flow_problems([{"request": "r", "status": "DONE"}, {"request": "r", "status": " Deferred "}]) == []
    assert ops.cross_flow_problems([{"request": "r", "status": "Wip"}])
    assert opt.normalize_cross_flow([{"request": "r", "status": " Deferred "}], "d")[0]["status"] == "deferred"


def test_provision_failure_detail_uses_optional_fields(proj):
    _job(proj, "op-p", [("run", "op", [{"active_s": 100}])], "2026-10-28", wall=400)
    t0 = _ts("2026-10-28", 11)
    _events(proj, [
        {"ts": t0, "event": "dispatch_provision_failed", "goal_id": "op-p", "task_id": "run",
         "pane_id": "w:p1", "provision_attempt": 0},
        {"ts": t0 + 1, "event": "dispatch_provision_failed", "goal_id": "op-p", "task_id": "run",
         "provision_attempt": 1, "stage": "create_pane", "error": "herdr 超时"}])
    c = _collect(proj)
    ev = c["framework_candidates"][0]["evidence"]
    assert ev[0].endswith("pane w:p1 第 0 次拉起失败")                      # 字段缺失时保持原文案
    assert ev[1].endswith("第 1 次拉起失败（create_pane：herdr 超时）") and "None" not in ev[1]
