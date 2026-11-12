"""产物结构契约（Task.artifact_contract）：载入期格式校验、fan-out 继承、DONE 收割时校验并失败关闭。

复现 gh-ainews-1102：同一 plan 的 x-detail 分片 thread 形态漂移（字符串数组 / 裸字符串 / 对象数组），
直到 build-tabbed-html 被人工打回才暴露。herdr 打桩，其余账本/信号/board 走真实代码。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from agent_team import coordinator, herdr, monitor
from agent_team.board import Board, DagError
from agent_team.config import Paths
from agent_team.models import (Task, Result, DISPATCHED, DONE, FAILED, PENDING, READY,
                               CONTRACT_ERROR_PREFIX)

ROOT = Path(__file__).resolve().parents[1]

THREAD_CONTRACT = {"glob": "x-detail-*.json", "root": "array",
                   "required": ["url", "thread"],
                   "fields": {"url": "string", "thread": "array<object>"}}


class FakeHerdr:
    def __init__(self):
        self.n = 0
        self.closed: list[str] = []

    def create_pane(self, *a, **k):
        self.n += 1
        return f"p{self.n}"

    def start_agent(self, pane_id, *a, **k):
        return f"h{pane_id}"

    def prompt_agent(self, target, text, *a, **k):
        pass

    def agent_status(self, target):
        return "working"

    def read_agent(self, target, lines=120, session=None, live=False):
        return ""

    def close_pane(self, pane_id):
        self.closed.append(pane_id)


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
def paths(tmp_path, fake):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    d = Paths(tmp_path).agent_dir("w1")
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml.safe_dump(
        {"name": "w1", "capabilities": ["x"], "max_concurrent": 1}), encoding="utf-8")
    (d / "prompt.md").write_text("persona", encoding="utf-8")
    return Paths(tmp_path)


def _board(paths, *tasks):
    b = Board("g", "goal", list(tasks))
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def _artifact(tmp_path, name, data) -> str:
    f = tmp_path / name
    f.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                 encoding="utf-8")
    return str(f)


def _deliver(paths, tid, artifacts, summary="ok"):
    d = paths.outbox("w1")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"g__{tid}.json").write_text(json.dumps(
        {"task_id": tid, "status": "done", "summary": summary, "artifacts": artifacts}),
        encoding="utf-8")


def _events(paths, name):
    f = paths.state_dir / "run.log"
    if not f.exists():
        return []
    return [e for e in map(json.loads, f.read_text(encoding="utf-8").splitlines())
            if e["event"] == name]


def _harvest(paths, tmp_path, contract, data, max_retries=1, name="x-detail-shard3.json"):
    """派发一个带契约的任务，worker 以 done 交付 data，再收割一轮。"""
    b = _board(paths, Task("shard", "抓详情", capabilities=["x"], max_retries=max_retries,
                           artifact_contract=contract))
    coordinator.tick(paths, b)
    assert b.tasks["shard"].status == DISPATCHED
    _deliver(paths, "shard", [_artifact(tmp_path, name, data)])
    assert coordinator.monitor_once(paths, b) == ["shard"]
    return b.tasks["shard"]


# ---------- 运行期校验（纯函数） ----------

def _v(contract, tmp_path, data, name="x-detail-shard1.json"):
    return monitor.contract_violations([contract], [_artifact(tmp_path, name, data)])


def test_compliant_array_passes(tmp_path):
    data = [{"url": "u1", "thread": [{"text": "a"}]}, {"url": "u2", "thread": []}]
    assert _v(THREAD_CONTRACT, tmp_path, data) == []


def test_root_type_mismatch(tmp_path):
    out = _v(THREAD_CONTRACT, tmp_path, {"items": []})
    assert len(out) == 1 and "根类型 期望 array 实际 object" in out[0]


def test_missing_required_field(tmp_path):
    out = _v(THREAD_CONTRACT, tmp_path, [{"url": "u"}, {"url": "u", "thread": None}])
    assert any("条目[0] 缺少必填字段 'thread'" in s for s in out)
    assert any("条目[1] 缺少必填字段 'thread'" in s for s in out)   # null 也算缺失
    assert len(out) == 2                                            # 缺失不再重复报类型错


def test_field_type_mismatch_reproduces_thread_drift(tmp_path):
    # shard3 的形态：thread 是裸字符串；shard1 的形态：字符串数组
    out = _v(THREAD_CONTRACT, tmp_path,
             [{"url": "u", "thread": "评论区更正…"}, {"url": "u", "thread": ["一条"]}])
    path = str(tmp_path / "x-detail-shard1.json")
    assert out == [f"{path}: 条目[0].thread 期望 array<object> 实际 string",
                   f"{path}: 条目[1].thread 期望 array<object> 实际 array 第0个元素为 string"]


def test_object_root_with_items_key_and_scalar_types(tmp_path):
    c = {"root": "object", "items_key": "items", "required": ["n"],
         "fields": {"n": "number", "ok": "boolean", "tags": "array<string>"}}
    assert _v(c, tmp_path, {"items": [{"n": 1, "ok": True, "tags": ["a"]}]}, "a.json") == []
    out = _v(c, tmp_path, {"items": [{"n": True, "ok": 1}]}, "a.json")
    assert any("items[0].n 期望 number 实际 boolean" in s for s in out)
    assert any("items[0].ok 期望 boolean 实际 number" in s for s in out)
    assert any("缺少条目数组键 'items'" in s for s in _v(c, tmp_path, {"rows": []}, "a.json"))


def test_no_matching_artifact_is_violation(tmp_path):
    other = _artifact(tmp_path, "notes.md", "x")
    out = monitor.contract_violations([THREAD_CONTRACT], [other])
    assert out == ["契约 glob='x-detail-*.json': 上报的 artifacts 中无匹配文件"]
    assert monitor.contract_violations([THREAD_CONTRACT], []) == out


def test_unreadable_or_invalid_json_is_violation(tmp_path):
    bad = _artifact(tmp_path, "x-detail-1.json", "not json")
    gone = str(tmp_path / "x-detail-2.json")
    out = monitor.contract_violations([THREAD_CONTRACT], [bad, gone, "x-detail-3.json"])
    assert "不是合法 JSON" in out[0] and "文件不存在" in out[1] and "非绝对路径" in out[2]


def test_violations_are_capped(tmp_path):
    out = _v(THREAD_CONTRACT, tmp_path, [{"url": "u", "thread": "s"}] * 30)
    assert len(out) == monitor.MAX_CONTRACT_VIOLATIONS + 1
    assert out[-1] == "…另有 10 项违约未列出"


# ---------- 收割：失败关闭 ----------

def test_harvest_compliant_is_done(paths, tmp_path):
    t = _harvest(paths, tmp_path, THREAD_CONTRACT, [{"url": "u", "thread": [{"t": "a"}]}])
    assert t.status == DONE and t.error is None
    assert _events(paths, "task_contract_violation") == []
    assert len(_events(paths, "task_done")) == 1


def test_harvest_violation_retries_then_fails(paths, tmp_path):
    t = _harvest(paths, tmp_path, THREAD_CONTRACT, [{"url": "u", "thread": "裸字符串"}],
                 max_retries=1)
    assert t.status == READY and t.agent == ""                   # 重试预算内：退回待派（已转 ready）
    assert t.error.startswith(CONTRACT_ERROR_PREFIX)
    assert "x-detail-shard3.json: 条目[0].thread 期望 array<object> 实际 string" in t.error
    assert t.runs[-1]["outcome"] == "failed"
    assert _events(paths, "task_done") == []
    assert _events(paths, "task_failed")[-1]["new_status"] == PENDING
    # 第二次仍违约：预算耗尽 → 终态失败
    b = Board.load(paths, "g")
    coordinator.tick(paths, b)
    assert b.tasks["shard"].status == DISPATCHED and b.tasks["shard"].attempts == 2
    _deliver(paths, "shard", [str(tmp_path / "x-detail-shard3.json")])
    coordinator.monitor_once(paths, b)
    assert b.tasks["shard"].status == FAILED
    assert len(_events(paths, "task_contract_violation")) == 2


def test_harvest_violation_terminal_without_retries(paths, tmp_path):
    t = _harvest(paths, tmp_path, THREAD_CONTRACT, {"thread": []}, max_retries=0)
    assert t.status == FAILED and "根类型 期望 array 实际 object" in t.error


def test_violation_event_fields(paths, tmp_path):
    _harvest(paths, tmp_path, THREAD_CONTRACT, [{"url": 1, "thread": "s"}])
    evs = _events(paths, "task_contract_violation")
    assert len(evs) == 1
    ev = evs[0]
    # optimize-contract-metrics 按事件名与这四个字段计数：字段集固定，不得增删改名
    assert set(ev) == {"ts", "event", "goal_id", "task_id", "agent", "violations"}
    assert (ev["goal_id"], ev["task_id"], ev["agent"]) == ("g", "shard", "w1")
    assert isinstance(ev["violations"], list) and all(isinstance(s, str) for s in ev["violations"])
    assert len(ev["violations"]) == 2


def test_retry_bootstrap_echoes_violation(paths, tmp_path):
    from agent_team import bootstrap
    from agent_team.registry import load_manifest
    t = _harvest(paths, tmp_path, THREAD_CONTRACT, [{"url": "u", "thread": "s"}])
    text = bootstrap.build_bootstrap(paths, load_manifest(paths, "w1"), "g", t)
    assert "产物结构契约" in text and "匹配 x-detail-*.json：根为 array" in text
    assert "上次交付违反该契约" in text and "thread 期望 array<object> 实际 string" in text


def test_no_contract_behavior_unchanged(paths, tmp_path):
    # 未声明契约：形态再怪也照常 DONE，只走原有的存在性哨兵
    b = _board(paths, Task("t", "g", capabilities=["x"]))
    assert b.tasks["t"].artifact_contract == []
    coordinator.tick(paths, b)
    _deliver(paths, "t", [_artifact(tmp_path, "x-detail-shard3.json", "\"bare\""),
                          str(tmp_path / "missing.json")])
    coordinator.monitor_once(paths, b)
    t = b.tasks["t"]
    assert t.status == DONE and t.error is None
    assert t.artifact_warnings == [str(tmp_path / "missing.json")]
    assert _events(paths, "task_contract_violation") == []


def test_direct_apply_done_without_contract(tmp_path):
    p = Paths(tmp_path)
    t = Task("t", "g", status=DISPATCHED)
    monitor._apply_done(p, "g", t, Result("t", "done", artifacts=["rel.json"]), time.time(), "w")
    assert t.status == DONE


# ---------- 载入期 / 展开 / 信号 ----------

def test_single_object_normalized_and_roundtrip():
    t = Task("t", "g", artifact_contract=THREAD_CONTRACT)
    assert t.artifact_contract == [THREAD_CONTRACT]
    assert Task.from_dict(t.to_dict()).artifact_contract == [THREAD_CONTRACT]
    assert Task("t", "g", artifact_contract=None).artifact_contract == []
    assert Task("t", "g", artifact_contract={}).artifact_contract == []


@pytest.mark.parametrize("contract, needle", [
    ("array", "必须是对象或对象列表"),
    ([{"root": "list"}], "root 必须是"),
    ([{"root": "array", "items_key": "items"}], "items_key 只能与 root=object"),
    ([{"root": "array", "fields": {"thread": "arr<object>"}}], "fields 类型非法"),
    ([{"root": "array", "fields": {"thread": "array<array>"}}], "fields 类型非法"),
    ([{"root": "array", "required": "url"}], "required 必须是"),
    ([{"root": "array", "glob": ""}], "glob 必须是非空字符串"),
    ([{"root": "array", "require": ["url"]}], "未知键"),
    ([42], "必须是对象"),
])
def test_invalid_contract_rejected_at_load(contract, needle):
    b = Board("g", "goal", [Task("t", "g", artifact_contract=contract)])
    with pytest.raises(DagError, match="非法产物契约") as ei:
        b.validate()
    assert needle in str(ei.value)


def test_expand_children_inherit_contract(paths):
    b = _board(paths, Task("up", "g", status=DONE),
               Task("fan", "抓第 {{shard}} 片", needs=["up"], capabilities=["x"],
                    fanout={"max_parallel": 2}, artifact_contract=THREAD_CONTRACT),
               Task("join", "g", needs=["fan"]))
    res = coordinator.expand_fanout(paths, b, "fan", ["g1", "g2"])
    assert res["ok"]
    for cid in res["children"]:
        assert b.tasks[cid].artifact_contract == [THREAD_CONTRACT]
    # 深拷贝：改一个子任务的契约不影响模板与兄弟
    b.tasks["fan.1"].artifact_contract[0]["root"] = "object"
    assert b.tasks["fan.2"].artifact_contract[0]["root"] == "array"
    assert b.tasks["fan"].artifact_contract[0]["root"] == "array"


def test_signal_carries_contract():
    sig = Task("t", "g", artifact_contract=THREAD_CONTRACT).to_signal([])
    assert sig["artifact_contract"] == [THREAD_CONTRACT]
    assert Task("t", "g").to_signal([])["artifact_contract"] == []


def test_existing_plans_unchanged_without_contract():
    """替代 `team plan --dry-run`（会写 coordinator/state）：现有 plan 模板逐个按 Board 校验 +
    构造信号，未声明契约时载入照常通过、契约为空、信号除新增空字段外键集不变。"""
    legacy_keys = {"task_id", "goal", "context_refs", "deps_done", "inherited_artifacts",
                   "reject_to", "rejections", "expect"}
    plans = sorted((ROOT / "coordinator" / "plans").glob("*.json"))
    if not plans:      # 公开仓只保留空 plans/ 目录（实例模板不入库），无既有 plan 可校验
        pytest.skip("coordinator/plans/ 下没有实例 plan 模板")
    for f in plans:
        data = json.loads(f.read_text(encoding="utf-8"))
        raw = data.get("tasks") or []
        tasks = [Task.from_dict({**t, "goal": t.get("goal") or t.get("instruction") or t.get("title")})
                 for t in raw]
        Board(data.get("goal_id", f.stem), data.get("goal", ""), tasks).validate()
        for t in tasks:
            assert t.artifact_contract == [], f"{f.name}:{t.task_id}"
            sig = t.to_signal([])
            assert set(sig) == legacy_keys | {"artifact_contract"}
            assert sig["artifact_contract"] == []


# ---------- 评审修复（r1） ----------

@pytest.mark.parametrize("contract", [
    ["array"],                                       # 条目不是对象
    [{"root": "array", "fields": ["thread"]}],       # fields 不是对象
    [{"root": "array", "required": "url"}],          # required 不是列表
    [{"root": "array", "fields": {"thread": "arr"}}],
])
def test_invalid_contract_at_harvest_is_violation_not_crash(tmp_path, contract):
    # 未经 Board.validate 的坏契约（如落盘态被改坏）：转成违约项，不抛异常中断整轮收割
    art = _artifact(tmp_path, "x.json", [{"thread": []}])
    out = monitor.contract_violations(contract, [art])
    assert out and all(s.startswith("契约格式非法: ") for s in out)


def test_invalid_contract_on_board_does_not_block_other_harvests(paths, tmp_path):
    b = _board(paths, Task("bad", "g", capabilities=["x"]), Task("good", "g", capabilities=["x"]))
    b.tasks["bad"].artifact_contract = ["array"]     # 绕过 validate 直接改坏
    paths.agent_dir("w1").joinpath("agent.yaml").write_text(yaml.safe_dump(
        {"name": "w1", "capabilities": ["x"], "max_concurrent": 2}), encoding="utf-8")
    assert sorted(coordinator.tick(paths, b)["dispatched"]) == ["bad", "good"]   # 坏契约不卡派发
    art = _artifact(tmp_path, "x.json", [])
    _deliver(paths, "bad", [art])
    _deliver(paths, "good", [art])
    assert sorted(coordinator.monitor_once(paths, b)) == ["bad", "good"]
    assert b.tasks["good"].status == DONE
    assert "契约格式非法" in b.tasks["bad"].error


def test_duplicate_artifacts_checked_once(tmp_path):
    art = _artifact(tmp_path, "x-detail-1.json", [{"url": "u", "thread": "s"}])
    assert len(monitor.contract_violations([THREAD_CONTRACT], [art, art, art])) == 1


def test_describe_contract_root_object_without_items_key():
    from agent_team.models import describe_contract
    s = describe_contract({"root": "object", "required": ["title"], "fields": {"n": "number"}})
    assert "根对象必填 ['title']" in s and "根对象字段类型" in s and "条目" not in s
    s = describe_contract({"root": "array", "required": ["url"]})
    assert "条目必填 ['url']" in s


def test_object_root_without_items_key_checks_root(tmp_path):
    c = {"root": "object", "required": ["title"], "fields": {"n": "number"}}
    assert _v(c, tmp_path, {"title": "t", "n": 1}, "a.json") == []
    out = _v(c, tmp_path, {"n": "1"}, "a.json")
    assert any("根对象 缺少必填字段 'title'" in s for s in out)
    assert any("根对象.n 期望 number 实际 string" in s for s in out)
