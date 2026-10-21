"""信号读写：状态归一化、JSON 打捞、裸名 outbox 采纳、信号清理/隔离。"""
from __future__ import annotations

import json
import os

import pytest

from agent_team import herdr, signals
from agent_team.config import Paths
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
])
def test_salvage_json_recovers(raw):
    assert signals.salvage_json(raw)["status"] == "done"


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "{broken"])
def test_salvage_json_gives_up(raw):
    assert signals.salvage_json(raw) is None


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

@pytest.mark.parametrize("raw, want", [
    ("Coder", "coder"), ("x scout:t1", "x-scout-t1"), ("1abc", "a1abc"), ("", "a"),
])
def test_sanitize_name_short(raw, want):
    assert herdr.sanitize_name(raw) == want


def test_sanitize_name_long_keeps_uniqueness():
    a = herdr.sanitize_name("very-long-agent-name-here:some-task-id:a1")
    b = herdr.sanitize_name("very-long-agent-name-here:some-task-id:a2")
    assert a != b and len(a) <= 32 and len(b) <= 32
