"""ledger.commit_merge：只回写本进程改动的 key，不用陈旧快照覆盖/复活其他 Job 的并发写。"""
from __future__ import annotations

import pytest

from agent_team import ledger
from agent_team.config import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path)


def _seed(paths):
    ledger.save(paths, {"A:t": {"agent": "w1", "dispatched_at": 1.0},
                        "B:t": {"agent": "w2", "dispatched_at": 1.0, "idle_since": 5.0}})
    rt = ledger.load(paths)
    return rt, ledger.snapshot(rt)


def test_untouched_key_keeps_concurrent_write(paths):
    rt, original = _seed(paths)
    disk = ledger.load(paths)                        # 并发写者(Job B 的 answer)重置时钟
    disk["B:t"].update(dispatched_at=9.0, idle_since=None)
    ledger.save(paths, disk)
    rt["A:t"]["dispatched_at"] = 2.0                 # 本进程只就地改 A
    ledger.commit_merge(paths, rt, original)
    got = ledger.load(paths)
    assert got["B:t"] == {"agent": "w2", "dispatched_at": 9.0, "idle_since": None}
    assert got["A:t"]["dispatched_at"] == 2.0


def test_untouched_key_stays_removed(paths):
    rt, original = _seed(paths)
    disk = ledger.load(paths)                        # 并发写者(Job B 的 skip)剔掉条目
    disk.pop("B:t")
    ledger.save(paths, disk)
    rt["A:t"]["dispatched_at"] = 2.0
    ledger.commit_merge(paths, rt, original)
    got = ledger.load(paths)
    assert "B:t" not in got                          # 不被陈旧快照复活
    assert got["A:t"]["dispatched_at"] == 2.0


def test_key_removed_in_memory_is_removed_on_disk(paths):
    rt, original = _seed(paths)
    disk = ledger.load(paths)
    disk["C:t"] = {"agent": "w3"}                    # 并发新增的条目照常存活
    ledger.save(paths, disk)
    rt.pop("A:t")
    ledger.commit_merge(paths, rt, original)
    got = ledger.load(paths)
    assert "A:t" not in got
    assert set(got) == {"B:t", "C:t"}
