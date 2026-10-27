"""信号读写：状态归一化、JSON 打捞、裸名 outbox 采纳、信号清理/隔离。"""
from __future__ import annotations

import json
import os
import re

import pytest

from agent_team import herdr, signals
from agent_team.config import Paths, read_json_or
from agent_team.models import DONE, FAILED, REJECT


@pytest.mark.parametrize("raw, want", [
    ("done", DONE), (" Completed ", DONE), ("ok", DONE),
    ("reject", REJECT), ("打回", REJECT), ("send-back", REJECT),
    ("failed", FAILED), ("", FAILED), (None, FAILED), ("weird", FAILED),
])
def test_normalize_status(raw, want):
    assert signals._normalize_status(raw) == want


@pytest.mark.parametrize("raw", [
    '{"status": "done"}',
    '```json\n{"status": "done"}\n```',
    'here you go: {"status": "done"} thanks',
    '集合 {50} 与模板 {x}，信号：{"status": "done", "note": "a } in str"}',
    '完成了 5" 屏幕适配 {"task_id":"t","status":"done"}',            # 散文杂散引号不吞信号
    '结果如下：\n```json\n{"status": "done", "meta": {"files": 3}}\n```\n以上。',
    '{"meta": {"status": "done"}, "oops": ',                         # 外层缺 status 时往里找
])
def test_salvage_json_recovers(raw):
    assert signals.salvage_json(raw)["status"] == "done"


def test_salvage_json_prefers_outermost_signal():
    d = signals.salvage_json('ok: {"status": "done", "meta": {"files": 3}} 谢谢')
    assert d == {"status": "done", "meta": {"files": 3}}


# 半截写入：外层 `{` 未闭合，唯一完整的是嵌套对象——不得当信号采纳（否则判 failed 白烧重试）
_TRUNCATED = '{"task_id":"t","status":"done","meta":{"files":3},"summary":"写到一半'


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "{broken", _TRUNCATED,
                                 '说明 {"files": 3} 无 status'])
def test_salvage_json_gives_up(raw):
    assert signals.salvage_json(raw) is None


def test_read_outbox_truncated_is_malformed(tmp_path):
    paths = Paths(tmp_path)
    _put(paths.outbox("a") / "g__t.json", _TRUNCATED)
    assert signals.read_outbox_ex(paths, "a", "g", "t") == (None, "malformed")


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path)


def _put(path, data, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_read_outbox_states(paths):
    assert signals.read_outbox_ex(paths, "a", "g", "t") == (None, "missing")
    f = _put(paths.outbox("a") / "g__t.json", "garbage")
    assert signals.read_outbox_ex(paths, "a", "g", "t") == (None, "malformed")
    _put(f, {"status": "success", "summary": "s", "artifacts": ["/x"], "retryable": False})
    res, state = signals.read_outbox_ex(paths, "a", "g", "t")
    assert state == "ok" and res.status == DONE and res.artifacts == ["/x"]
    assert res.retryable is False and res.task_id == "t" and res.written_at is not None


@pytest.mark.parametrize("payload, field, want", [
    ({"artifacts": None}, "artifacts", []),                      # null 不再 list(None) 抛 TypeError
    ({"artifacts": "/tmp/a.md"}, "artifacts", ["/tmp/a.md"]),    # 单串不拆成字符
    ({"artifacts": ["/a", None, "", 3]}, "artifacts", ["/a", "3"]),
    ({"artifacts": {"k": "v"}}, "artifacts", []),
    ({"retryable": "false"}, "retryable", False),
    ({"retryable": "NO"}, "retryable", False),
    ({"retryable": "0"}, "retryable", False),
    ({"retryable": "True"}, "retryable", True),
    ({"retryable": "yes"}, "retryable", True),
    ({"retryable": "maybe"}, "retryable", None),
    ({"retryable": None}, "retryable", None),
    ({"summary": None}, "summary", ""),
    ({"summary": 42}, "summary", "42"),
    ({"error": None}, "error", None),
    ({"error": {"code": 1}}, "error", "{'code': 1}"),
    ({"task_id": None}, "task_id", "t"),
])
def test_read_outbox_coerces_field_types(paths, payload, field, want):
    _put(paths.outbox("a") / "g__t.json", {"status": "failed", **payload})
    res, state = signals.read_outbox_ex(paths, "a", "g", "t")
    assert state == "ok" and getattr(res, field) == want
    if isinstance(want, bool) or want is None:
        assert getattr(res, field) is want


# 半截写入切断中文 3 字节字符：解码失败也要按 malformed / default 处理，不能抛 UnicodeDecodeError
_TRUNCATED_UTF8 = b'{"summary":"\xe5\xae'


def test_read_outbox_truncated_multibyte_is_malformed(paths):
    f = paths.outbox("a") / "g__t.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(_TRUNCATED_UTF8)
    assert signals.read_outbox_ex(paths, "a", "g", "t") == (None, "malformed")


def test_read_json_or_truncated_multibyte_returns_default(tmp_path):
    f = tmp_path / "state.json"
    f.write_bytes(_TRUNCATED_UTF8)
    assert read_json_or(f, "D") == "D"
    f.write_text("{broken", encoding="utf-8")
    assert read_json_or(f, "D") == "D"


def test_bare_outbox_adopted_only_if_newer(paths):
    bare = _put(paths.outbox("a") / "t.json", {"status": "done"}, mtime=100.0)
    assert signals.read_outbox_ex(paths, "a", "g", "t", since=200.0) == (None, "missing")
    assert signals.read_outbox_ex(paths, "a", "g", "t") == (None, "missing")   # 无 since 不采纳
    os.utime(bare, (300.0, 300.0))
    res, state = signals.read_outbox_ex(paths, "a", "g", "t", since=200.0)
    assert state == "ok" and res.status == DONE
    assert not bare.exists() and (paths.outbox("a") / "g__t.json").exists()   # 已归位为规范名


def test_write_inbox_and_clear(paths):
    inb = signals.write_inbox(paths, "a", "g", {"task_id": "t", "goal": "x"})
    assert inb.name == "g__t.json" and json.loads(inb.read_text())["goal"] == "x"
    out = _put(paths.outbox("a") / "g__t.json", {"status": "done"})
    signals.clear_task_signals(paths, "a", "g", "t")
    assert not inb.exists() and not out.exists()
    signals.clear_task_signals(paths, "a", "g", "t")   # 再清一次：无文件也不抛


def test_clear_quarantines_malformed(paths):
    _put(paths.outbox("a") / "g__t.json", "garbage")
    signals.clear_task_signals(paths, "a", "g", "t", quarantine_outbox=True)
    kept = list(paths.outbox("a").glob("g__t.malformed.*.json"))
    assert len(kept) == 1 and kept[0].read_text() == "garbage"


# ---------- herdr.sanitize_name ----------

_HERDR_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@pytest.mark.parametrize("raw", ["coder", "x-scout-t1", "a1abc", "w_g-t1-a1p0", "a" * 32])
def test_sanitize_name_clean_unchanged(raw):
    assert herdr.sanitize_name(raw) == raw          # 本就合法的短名原样返回（幂等）


@pytest.mark.parametrize("raw, head", [
    ("Coder", "coder-"), ("x scout:t1", "x-scout-t1-"), ("1abc", "a1abc-"), ("", "a-"),
])
def test_sanitize_name_lossy_keeps_readable_head(raw, head):
    out = herdr.sanitize_name(raw)
    assert out.startswith(head) and len(out) == len(head) + 6


@pytest.mark.parametrize("a, b", [
    ("w:g:tpl.1:a1p0", "w:g:tpl-1:a1p0"),   # fan-out 子任务 vs plan 任务
    ("Build", "build"),                      # 仅大小写不同
    ("1abc", "a1abc"),                       # 补首字母后同形
    ("-abc", "abc"),                         # 去首尾 - 后同形
])
def test_sanitize_name_lossy_no_collision(a, b):
    assert herdr.sanitize_name(a) != herdr.sanitize_name(b)


def test_sanitize_name_long_keeps_uniqueness():
    a = herdr.sanitize_name("very-long-agent-name-here:some-task-id:a1")
    b = herdr.sanitize_name("very-long-agent-name-here:some-task-id:a2")
    assert a != b and len(a) <= 32 and len(b) <= 32


@pytest.mark.parametrize("raw", [
    "", "-", "---", "9", "123abc", "_x", "Coder", "x scout:t1", "w:g:tpl.1:a1p0",
    "智能体:任务", "émoji🚀:t1", "a" * 32, "a" * 33, "A" * 100,
    "very-long-agent-name-here:some-task-id:a1", "x" * 24 + "-" + "y" * 20,
    "scout:" + "g" * 40 + ":t1:a1p0",
])
def test_sanitize_name_always_valid(raw):
    out = herdr.sanitize_name(raw)
    assert _HERDR_NAME_RE.match(out), out
    assert herdr.sanitize_name(out) == out           # 输出已合法 → 再过一遍不变
