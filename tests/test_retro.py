"""流程复盘（自我增强）：收敛 Job → 复盘 Job 的生成/去重/跟随派发，以及建议待办池的收入/排序/标记。

herdr 打桩同 test_coordinator；其余（board 落盘、run.log、信号、产物目录）走真实代码。
"""
from __future__ import annotations

import json

import yaml

from agent_team import cli, coordinator, retro
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DONE, DISPATCHED

import pytest
from test_coordinator import _agent, fake  # noqa: F401 —— fake 是 fixture，paths 依赖它打桩 herdr


@pytest.fixture
def paths(tmp_path, fake, monkeypatch):  # noqa: F811
    (tmp_path / "team.yaml").write_text("agents: []\nretro:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Paths(tmp_path)


def _outbox(paths, agent, gid, tid, data):
    d = paths.outbox(agent)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{gid}__{tid}.json").write_text(json.dumps({"task_id": tid, **data}), encoding="utf-8")


def _job(paths, gid="g", flag=True):
    b = Board(gid, "做个东西", [Task("t1", "do it", capabilities=["x"])])
    b.retro = flag
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def _run_to_done(paths, b, agent="w1"):
    coordinator.tick(paths, b)
    _outbox(paths, agent, b.goal_id, "t1", {"status": "done", "summary": "ok"})
    return coordinator.tick(paths, b)


def _retro_done(paths, rgid, suggestions, source="g"):
    """模拟 flow-retro worker 交付：写 suggestions.json 再写 outbox。"""
    art = paths.task_artifacts_dir(rgid, retro.RETRO_TASK)
    art.mkdir(parents=True, exist_ok=True)
    f = art / retro.SUGGESTIONS_FILE
    f.write_text(json.dumps({"source_goal_id": source, "verdict": "v", "suggestions": suggestions},
                            ensure_ascii=False), encoding="utf-8")
    _outbox(paths, "retro", rgid, retro.RETRO_TASK,
            {"status": "done", "summary": f"{len(suggestions)} 条", "artifacts": [str(f)]})
    return f


SUG = {"title": "x-scout prompt 写清 outbox 文件名", "scope": "flow", "category": "agent-prompt",
       "target": "agents/w1/prompt.md", "problem": "写错名", "evidence": ["task_failed delivery missing"],
       "proposal": "加一句", "impact": "high", "effort": "low", "confidence": 0.9, "dup_of": None}


# ---------- 自动复盘 ----------

def test_completion_spawns_and_dispatches_retro_job(paths):
    _agent(paths, "w1")
    _agent(paths, "retro", caps=("flow-retro",))
    b = _job(paths)
    res = _run_to_done(paths, b)
    assert res["complete"]
    [rgid] = res["retro_spawned"]
    assert rgid.startswith("retro-g-") and res["retro_spawned"][rgid] == [retro.RETRO_TASK]
    rb = Board.load(paths, rgid)
    t = rb.tasks[retro.RETRO_TASK]
    assert t.status == DISPATCHED and t.agent == "retro"
    inp = paths.job_artifacts_dir(rgid) / "_input"
    assert json.loads((inp / "report.json").read_text())["goal_id"] == "g"
    events = [json.loads(x) for x in (inp / "events.jsonl").read_text().splitlines()]
    assert events and all(e["goal_id"] == "g" for e in events)
    assert str(inp / "report.json") in t.goal and "agents/w1/prompt.md" in t.goal.replace(str(paths.root) + "/", "")
    # 幂等：再 tick 不会再建；复盘 Job 自己收敛也不再复盘
    assert coordinator.tick(paths, b)["retro_spawned"] == {}
    _retro_done(paths, rgid, [SUG])
    res = coordinator.tick(paths, rb)
    assert res["complete"] and res["retro_spawned"] == {} and res["insights_new"] == 1
    assert len([x for x in Board.load_all(paths) if retro.is_retro(x.goal_id)]) == 1


def test_no_retro_without_agent_or_when_either_switch_off(paths):
    _agent(paths, "w1")
    assert _run_to_done(paths, _job(paths, "g"))["retro_spawned"] == {}       # 无 flow-retro agent
    _agent(paths, "retro", caps=("flow-retro",))
    assert _run_to_done(paths, _job(paths, "g2", flag=False))["retro_spawned"] == {}   # 流程开关关
    (paths.root / "team.yaml").write_text(yaml.safe_dump({"agents": [], "retro": {"enabled": False}}))
    assert _run_to_done(paths, _job(paths, "g3"))["retro_spawned"] == {}      # 全局总闸关


def test_defaults_are_off(tmp_path, monkeypatch, fake):  # noqa: F811
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    p = Paths(tmp_path)
    assert not retro.globally_enabled(p)
    b = Board("old", "g", [Task("t", "x")])
    d = b.to_dict()
    d.pop("retro")                                   # 历史 board 没有这个字段
    assert Board.from_dict(d).retro is False


def test_dry_run_jobs_are_not_retroed(paths):
    _agent(paths, "w1")
    _agent(paths, "retro", caps=("flow-retro",))
    b = _job(paths)
    coordinator.tick(paths, b, dry_run=True)
    assert b.tasks["t1"].runs[-1]["dry_run"] is True
    _outbox(paths, "w1", "g", "t1", {"status": "done", "summary": "ok"})
    coordinator.monitor_once(paths, b, close_panes=False)
    assert b.is_complete() and retro.sweep(paths) == []


def test_turning_on_completed_job_retros_it_once(paths):
    _agent(paths, "w1")
    _agent(paths, "retro", caps=("flow-retro",))
    b = _job(paths, flag=False)
    _run_to_done(paths, b)
    assert retro.sweep(paths) == []
    assert cli.main(["retro", "on", "--job", "g"]) == 0
    [rgid] = retro.sweep(paths)
    assert rgid.startswith("retro-g-") and retro.sweep(paths) == []
    with pytest.raises(SystemExit):
        cli.main(["retro", "on", "--job", rgid])     # 复盘 Job 不能再开复盘


def test_run_loop_follows_retro_job(paths):
    _agent(paths, "w1")
    _agent(paths, "retro", caps=("flow-retro",))
    b = _job(paths)
    coordinator.tick(paths, b)
    _outbox(paths, "w1", "g", "t1", {"status": "done", "summary": "ok"})
    res = coordinator.run_loop(paths, interval=0, max_ticks=2, goal_id="g")
    # g 收敛但复盘还在跑 → run 不提前宣告完成
    assert res["status"] == "max_ticks"
    rgid = next(x for x in res["jobs"] if retro.is_retro(x))
    _retro_done(paths, rgid, [])
    assert coordinator.run_loop(paths, interval=0, max_ticks=5, goal_id="g")["status"] == "complete"


# ---------- 建议池 ----------

def _seed_retro_job(paths, rgid, suggestions, source="g"):
    rb = Board(rgid, "复盘", [Task(retro.RETRO_TASK, "x", capabilities=["flow-retro"], status=DONE)])
    rb.save(paths)
    f = _retro_done(paths, rgid, suggestions, source)
    rb.tasks[retro.RETRO_TASK].artifacts = [str(f)]
    rb.save(paths)


def test_ingest_dedupes_recurs_and_reopens(paths):
    _seed_retro_job(paths, "retro-g-1", [SUG, {"title": "低价值", "impact": "low", "effort": "high"}])
    st = retro.ingest(paths)
    assert (st["new"], st["recurred"]) == (2, 0)
    assert retro.ingest(paths)["sources"] == []          # 同一份不重复收入
    items = retro.ranked(paths)
    assert items[0]["title"] == SUG["title"] and items[0]["score"] > items[1]["score"]
    sid = items[0]["id"]
    # 另一次复盘报同一问题（指纹相同）→ 复现计数，得分上涨
    _seed_retro_job(paths, "retro-g2-1", [dict(SUG, evidence=["新证据"])], source="g2")
    assert retro.ingest(paths)["recurred"] == 1
    it = retro.ranked(paths)[0]
    assert it["id"] == sid and it["occurrences"] == 2 and "新证据" in it["evidence"]
    assert it["score"] > items[0]["score"]
    # 已采纳后又被复盘报出（dup_of 指向）→ 自动重开
    assert retro.mark(paths, [sid], "applied", note="改了 prompt") == []
    assert retro.ranked(paths, status="applied")[0]["note"] == "改了 prompt"
    _seed_retro_job(paths, "retro-g3-1", [{"title": "换个说法", "dup_of": sid}], source="g3")
    assert retro.ingest(paths)["reopened"] == 1
    assert retro.ranked(paths)[0]["id"] == sid and retro.ranked(paths)[0]["occurrences"] == 3


def test_ingest_tolerates_malformed(paths):
    rb = Board("retro-bad-1", "复盘", [Task(retro.RETRO_TASK, "x", status=DONE)])
    rb.save(paths)
    art = paths.task_artifacts_dir("retro-bad-1", retro.RETRO_TASK)
    art.mkdir(parents=True)
    (art / retro.SUGGESTIONS_FILE).write_text("{not json")
    st = retro.ingest(paths)
    assert st["malformed"] and retro.ranked(paths) == []


def test_cli_retro_and_insights(paths, capsys):
    _agent(paths, "w1")
    b = _job(paths)
    _run_to_done(paths, b)                               # 无 flow-retro agent：不自动复盘
    with pytest.raises(SystemExit):
        cli.main(["retro", "run", "g"])
    _agent(paths, "retro", caps=("flow-retro",))
    assert cli.main(["retro", "run", "g"]) == 0
    rgid = next(x.goal_id for x in Board.load_all(paths) if retro.is_retro(x.goal_id))
    assert retro.sweep(paths) == []                      # 手动复盘已记账，sweep 不再重复建
    coordinator.tick(paths, Board.load(paths, rgid))
    _retro_done(paths, rgid, [SUG])
    coordinator.monitor_once(paths, Board.load(paths, rgid))
    capsys.readouterr()
    assert cli.main(["insights", "list"]) == 0
    out = capsys.readouterr().out
    assert "新增 1 条" in out and SUG["title"] in out
    sid = retro.ranked(paths)[0]["id"]
    assert cli.main(["insights", "mark", "dismissed", sid, "--note", "不需要"]) == 0
    assert cli.main(["insights", "mark", "applied", "ins-nope"]) == 1
    assert retro.ranked(paths) == [] and retro.ranked(paths, status="dismissed")[0]["note"] == "不需要"


# ---------- 开关 ----------

def test_cli_toggles_global_and_plan_flag(paths, capsys):
    assert cli.main(["retro", "off"]) == 0
    assert not retro.globally_enabled(paths)
    assert cli.main(["retro", "on"]) == 0
    assert retro.globally_enabled(paths)
    paths.plans_dir.mkdir(parents=True)
    f = paths.plans_dir / "demo.json"
    f.write_text('{\n  "goal_id": "demo",\n  "tasks": [{"task_id": "a", "goal": "x", "capabilities": ["x"]}]\n}\n',
                 encoding="utf-8")
    assert cli.main(["retro", "on", "demo"]) == 0
    assert json.loads(f.read_text())["retro"] is True
    assert '"tasks": [{"task_id": "a"' in f.read_text()          # 只插一行，不重排原有格式
    assert cli.main(["plan", str(f)]) == 0
    assert Board.load(paths, "demo").retro is True
    assert cli.main(["retro", "off", "demo"]) == 0
    assert json.loads(f.read_text())["retro"] is False and f.read_text().count('"retro"') == 1
    capsys.readouterr()
    assert cli.main(["retro", "status"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["enabled"] is True and v["plans"] == {"demo": False} and v["jobs"] == {"demo": True}


def test_plan_rejects_non_bool_retro(paths):
    f = paths.root / "p.json"
    f.write_text(json.dumps({"goal_id": "p", "retro": "yes",
                             "tasks": [{"task_id": "a", "goal": "x", "capabilities": ["x"]}]}))
    with pytest.raises(SystemExit):
        cli.main(["plan", str(f)])
