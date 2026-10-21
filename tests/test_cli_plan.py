"""CLI 的 plan 载入：模板填槽、漏填守卫、id 校验、DAG 校验；以及看板产物路径越权校验。

在 tmp 项目根（含 team.yaml）里跑 `cli.main`，不碰真实 herdr（载入期无在途 pane）。
"""
from __future__ import annotations

import json

import pytest

from agent_team import cli
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.dashboard import _artifact_target
from agent_team.models import READY, PENDING


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _plan(proj, data, name="p.json"):
    f = proj / name
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(f)


def _tpl(**over):
    d = {"goal_id": "demo", "goal": "{{topic}}",
         "tasks": [{"task_id": "a", "goal": "调研 {{topic}}", "capabilities": ["x"]},
                   {"task_id": "b", "instruction": "汇总", "needs": ["a"], "capabilities": ["x"]}]}
    d.update(over)
    return d


# ---------- 纯函数 ----------

def test_substitute_and_collect():
    data = {"a": "{{x}} and {{ y }}", "b": ["{{x}}", 1], "{{k}}": "keep-key"}
    assert cli._collect_placeholders(data) == {"x", "y"}
    out = cli._substitute(data, {"x": "1"})
    assert out == {"a": "1 and {{ y }}", "b": ["1", 1], "{{k}}": "keep-key"}


def test_collect_leftover_catches_irregular_tokens():
    assert cli._collect_leftover({"g": "{{a:b}} {{}} {{ok}}"}) == ["{{a:b}}", "{{}}", "{{ok}}"]
    assert cli._collect_placeholders({"g": "{{a:b}} {{}}"}) == set()


def test_fanout_slot_names():
    data = {"tasks": [{"task_id": "f", "fanout": {"max_parallel": 2, "shard_var": "i"}}, {"task_id": "x"}]}
    assert cli._fanout_slot_names(data) == {"i", "total"}


# ---------- cmd_plan ----------

def test_plan_loads_with_vars_and_alias(proj, capsys):
    f = _plan(proj, _tpl())
    assert cli.main(["plan", f, "--var", "topic=AI", "--as", "demo-1"]) == 0
    b = Board.load(Paths(proj), "demo-1")
    assert b.goal == "AI" and b.tasks["a"].goal == "调研 AI"
    assert b.tasks["b"].goal == "汇总"                     # instruction 别名
    assert b.tasks["a"].status == READY and b.tasks["b"].status == PENDING
    assert "loaded job demo-1" in capsys.readouterr().out


def test_plan_vars_file_and_show_vars(proj, capsys):
    f = _plan(proj, _tpl())
    assert cli.main(["plan", f, "--show-vars"]) == 0
    assert json.loads(capsys.readouterr().out)["placeholders"] == ["topic"]
    vf = proj / "v.json"
    vf.write_text(json.dumps({"topic": "多行\n目标"}), encoding="utf-8")
    assert cli.main(["plan", f, "--vars-file", str(vf)]) == 0
    assert Board.load(Paths(proj), "demo").tasks["a"].goal == "调研 多行\n目标"


def test_plan_fanout_shard_vars_exempt(proj):
    data = _tpl(tasks=[{"task_id": "f", "goal": "第 {{shard}}/{{total}} 片", "capabilities": ["x"],
                        "fanout": {"max_parallel": 2}}], goal="g")
    assert cli.main(["plan", _plan(proj, data)]) == 0


@pytest.mark.parametrize("data, args, err", [
    (_tpl(), [], "未填充"),
    (_tpl(goal="g", tasks=[{"task_id": "a", "goal": "{{a:b}}"}]), [], "未填充"),
    (_tpl(goal="g", tasks=[{"task_id": "a", "goal": "x"}, {"task_id": "a", "goal": "y"}]), [], "重复 task_id"),
    (_tpl(goal="g", tasks=[{"task_id": "a b", "goal": "x"}]), [], "只能含字母"),
    (_tpl(goal="g", tasks=[{"task_id": "a"}]), [], "缺少必填字段"),
    (_tpl(goal="g", tasks=[{"task_id": "a", "goal": "x", "needs": ["z"]}]), [], "悬空依赖"),
    (_tpl(goal="g", tasks=[{"task_id": "a", "goal": "x", "status": "bad"}]), [], "状态非法"),
    (_tpl(goal_id="bad id", goal="g"), ["--var", "topic=x"], "goal_id"),
    ({"topic": "x"}, [], "缺少非空 tasks"),
    (_tpl(), ["--var", "novalue"], "key=value"),
])
def test_plan_rejects(proj, capsys, data, args, err):
    with pytest.raises(SystemExit) as ei:
        cli.main(["plan", _plan(proj, data), *args])
    assert ei.value.code == 2 and err in capsys.readouterr().err
    assert not Paths(proj).jobs_dir.exists() or not list(Paths(proj).jobs_dir.glob("*.json"))


def test_plan_reload_purges_stale_signals_and_runtime(proj):
    p = Paths(proj)
    f = _plan(proj, _tpl())
    assert cli.main(["plan", f, "--var", "topic=x"]) == 0
    stale = p.outbox("w") / "demo__a.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    other = p.outbox("w") / "other__a.json"
    other.write_text("{}", encoding="utf-8")
    rt = p.state_dir / "runtime.json"
    rt.write_text(json.dumps({"demo:a": {"pane_id": "<dry-run>", "agent": "w"},
                              "other:a": {"pane_id": "<dry-run>", "agent": "w"}}), encoding="utf-8")
    assert cli.main(["plan", f, "--var", "topic=x"]) == 0
    assert not stale.exists() and other.exists()
    assert list(json.loads(rt.read_text())) == ["other:a"]


# ---------- dashboard 产物路径 ----------

def test_artifact_target_confines_to_root(tmp_path):
    root = tmp_path / "shared" / "artifacts"
    (root / "g").mkdir(parents=True)
    inside = root / "g" / "r.md"
    assert _artifact_target(root, str(inside)) == inside.resolve()
    assert _artifact_target(root, str(root)) == root.resolve()
    assert _artifact_target(root, str(root / ".." / "secret")) is None
    assert _artifact_target(root, "%2Fetc%2Fpasswd") is None
    assert _artifact_target(root, str(tmp_path / "artifacts-evil" / "x")) is None


# ---------- dashboard HTTP 路由 ----------

def test_dashboard_routes(proj, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from agent_team import dashboard
    f = _plan(proj, _tpl())
    assert cli.main(["plan", f, "--var", "topic=x"]) == 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._make_handler(Paths(proj)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        code, body = get("/api/state")
        assert code == 200 and "error" not in json.loads(body)
        code, body = get("/api/job?goal_id=demo")
        assert code == 200 and json.loads(body)["goal_id"] == "demo"
        assert json.loads(get("/api/job?goal_id=nope")[1])["error"].startswith("job not found")
        assert json.loads(get("/api/agent?name=ghost")[1])["error"].startswith("agent not found")
        assert get("/api/agent_tail")[0] == 400
        assert get("/api/jobs")[0] == 404                 # 精确路由：不被 /api/job 前缀吞掉
        assert get("/file?p=%2Fetc%2Fpasswd")[0] == 403
        assert get("/")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 项目根定位 ----------

def test_project_root_cwd_then_package_fallback(tmp_path, monkeypatch):
    from agent_team import config
    proj, sub, elsewhere = tmp_path / "proj", tmp_path / "proj" / "a" / "b", tmp_path / "elsewhere"
    sub.mkdir(parents=True)
    elsewhere.mkdir()
    (proj / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    monkeypatch.chdir(sub)
    assert config.project_root() == proj                      # cwd 向上查找优先
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config, "_PACKAGE_ROOT", proj)
    assert config.project_root() == proj                      # cwd 找不到：回退包所在仓库
    monkeypatch.setattr(config, "_PACKAGE_ROOT", tmp_path / "site-packages")
    assert config.project_root() == elsewhere                 # 都没有：用 cwd
