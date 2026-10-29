"""裸名 outbox 采纳的归属约束：同模板多 Job 并发跑同名 task 时，裸名交付归属不明 → 隔离、不采纳。

端到端用例的 herdr 打桩同 test_coordinator；其余（runtime 账本、信号文件、board 落盘）走真实代码。
"""
from __future__ import annotations

import json
import os

import pytest

from agent_team import coordinator, signals
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DONE, DISPATCHED
from test_coordinator import _agent, clock, fake  # noqa: F401 —— fixture，coord_paths 依赖 fake 打桩 herdr


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path)


def _put(path, data, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _ambiguous(paths, agent, task_id):
    return sorted(p.name for p in paths.outbox(agent).glob(f"{task_id}.ambiguous.*.json"))


def test_bare_outbox_ambiguous_is_quarantined(paths):
    # job1、job2 同模板，都在 agent a 上跑 fetch：两份 inbox 在途信号 + 一份新鲜裸名交付
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    signals.write_inbox(paths, "a", "job2", {"task_id": "fetch", "goal": "x"})
    bare = _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    assert signals.read_outbox_ex(paths, "a", "job2", "fetch", since=200.0) == (None, "missing")
    assert not bare.exists() and len(_ambiguous(paths, "a", "fetch")) == 1   # 已隔离取证
    assert signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0) == (None, "missing")
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


@pytest.mark.parametrize("goal", ["job1", "job2"])
def test_bare_outbox_double_underscore_task_not_counted(paths, goal):
    # task_id 允许含 `__`：job?__x__fetch.json（task=x__fetch）文件名也以 __fetch.json 结尾，
    # 须按信号体 task_id 精确比较——自己 goal 或别的 goal 的 x__fetch 都不阻断 job1 采纳 fetch
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    signals.write_inbox(paths, "a", goal, {"task_id": "x__fetch", "goal": "x"})
    bare = _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    res, state = signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0)
    assert state == "ok" and res.status == DONE
    assert not bare.exists() and not _ambiguous(paths, "a", "fetch")


def test_bare_outbox_unreadable_inbox_falls_back_to_filename(paths):
    # 别的 goal 的 inbox 信号读不出（半截写入）→ 按文件名回退，保守判归属不明
    signals.write_inbox(paths, "a", "job1", {"task_id": "fetch", "goal": "x"})
    _put(paths.inbox("a") / "job2__fetch.json", '{"task_id": "fe')
    _put(paths.outbox("a") / "fetch.json", {"status": "done"}, mtime=300.0)

    assert signals.read_outbox_ex(paths, "a", "job1", "fetch", since=200.0) == (None, "missing")
    assert len(_ambiguous(paths, "a", "fetch")) == 1


# ---- 端到端回归：owner 被判交付缺失、inbox 被 reclaim 清掉后，另一方不得把裸名交付领走 ----

@pytest.fixture
def coord_paths(tmp_path, fake):  # noqa: F811
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    return Paths(tmp_path)


def _job(paths, gid):
    b = Board(gid, "goal", [Task("fetch", "g", capabilities=["x"], max_retries=1)])
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def test_bare_outbox_not_stolen_after_owner_reclaimed(coord_paths, fake, clock):  # noqa: F811
    paths = coord_paths
    _agent(paths, "w1", maxc=1)
    b1, b2 = _job(paths, "job1"), _job(paths, "job2")
    coordinator.tick(paths, b1)          # job1 → pane p1 / hp1
    clock.tick(1)
    coordinator.tick(paths, b2)          # job2 → pane p2 / hp2
    assert b1.tasks["fetch"].status == DISPATCHED and b2.tasks["fetch"].status == DISPATCHED
    clock.tick(5)
    # job1 的 worker 写了裸名后 idle；job2 的 worker 仍在干活
    bare = _put(paths.outbox("w1") / "fetch.json", {"status": "done", "summary": "JOB1 DATA"},
                mtime=clock.t)
    fake.status["hp1"] = "idle"

    coordinator.monitor_once(paths, b1)  # 归属不明 → 隔离；job1 进入 idle 宽限
    coordinator.monitor_once(paths, b2)
    assert not bare.exists() and len(_ambiguous(paths, "w1", "fetch")) == 1
    clock.tick(11)
    coordinator.monitor_once(paths, b1)  # job1 超宽限判交付缺失 → reclaim 清掉 job1 inbox
    assert b1.tasks["fetch"].status != DONE and "p1" in fake.closed
    coordinator.monitor_once(paths, b2)  # 裸名已隔离：job2 无从领走 job1 的交付

    t2 = b2.tasks["fetch"]
    assert t2.status == DISPATCHED and t2.result_summary != "JOB1 DATA"
    assert "p2" not in fake.closed
    assert not (paths.outbox("w1") / "job2__fetch.json").exists()
