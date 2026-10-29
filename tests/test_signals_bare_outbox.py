"""裸名 outbox 采纳的归属约束：同模板多 Job 并发跑同名 task 时，裸名交付归属不明、不采纳。"""
from __future__ import annotations

import json
import os

import pytest

from agent_team import signals
from agent_team.config import Paths
from agent_team.models import DONE


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path)


def _put(path, data, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_bare_outbox_ambiguous_when_other_goal_inflight(paths):
    # job1、job2 同模板，都在 agent a 上跑 fetch：两份 inbox 在途信号 + 一份新鲜裸名交付
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    signals.write_inbox(paths, "a", "job2", {"task_id": "fetch", "goal": "x"})
    bare = _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    assert signals.read_outbox_ex(paths, "a", "job2", "fetch", since=200.0) == (None, "missing")
    assert signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0) == (None, "missing")
    assert bare.exists()   # 归属不明：原地保留，不改名
    assert not (paths.outbox("a") / "job1__fetch.json").exists()
    assert not (paths.outbox("a") / "job2__fetch.json").exists()


def test_bare_outbox_adopted_when_only_own_goal_inflight(paths):
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    signals.write_inbox(paths, "a", "job1", {"task_id": "other", "goal": "x"})   # 别的 task 不干扰
    bare = _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    res, state = signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0)
    assert state == "ok" and res.status == DONE
    assert not bare.exists() and (paths.outbox("a") / "job1__fetch.json").exists()


def test_bare_outbox_other_task_suffix_not_counted(paths):
    # 别的 goal 的同后缀但不同 task（如 refetch）不算同 task 在途，不阻断采纳
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    signals.write_inbox(paths, "a", "job2", {"task_id": "refetch", "goal": "x"})
    _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    res, state = signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0)
    assert state == "ok" and res.status == DONE
