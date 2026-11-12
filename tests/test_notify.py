"""Job 级关键节点提醒：开始 / 需人介入 / 打回 / 结束各一条，单个 worker 完成不提醒。

herdr.notify 由 conftest 的 autouse fixture `notifications` 打桩记录。
"""
from __future__ import annotations

from agent_team import interventions, notify
from agent_team.board import Board
from agent_team.config import Paths
from agent_team.models import Task, DONE, FAILED

import pytest


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    return Paths(tmp_path)


def _board(paths, gid="g"):
    b = Board(gid, "goal", [Task("a", "g"), Task("b", "g", needs=["a"])])
    b.validate()
    b.refresh_ready()
    b.save(paths)
    return b


def test_finish_notifies_once_not_per_task(paths, notifications):
    b = _board(paths)
    b.tasks["a"].status = DONE
    b.save(paths)                                   # 只完成一个节点：不提醒
    assert notifications == []
    b.tasks["b"].status = DONE
    b.save(paths)
    b.save(paths)                                   # 已收敛后再落盘不重复提醒
    assert notifications == [("流程结束 · g", "done=2", "done")]


def test_finish_with_failures_says_so(paths, notifications):
    b = _board(paths)
    b.tasks["a"].status = FAILED
    b.refresh_ready()                               # 下游级联 skipped → 收敛
    b.save(paths)
    assert notifications[-1][0] == "流程结束（有失败） · g"


def test_reject_notifies_and_reopened_job_notifies_again(paths, notifications):
    b = _board(paths)
    for t in b.tasks.values():
        t.status = DONE
    b.save(paths)
    out = interventions.apply_reject(paths, b, "a", from_task="b", reason="字段缺失")
    assert out["applied"] == ["a"]
    assert ("打回 · g", "a（来自 b）: 字段缺失", "none") in notifications
    for t in b.tasks.values():
        t.status = DONE
    b.save(paths)                                   # 打回重跑后再次收敛，再提醒一次
    assert [n[0] for n in notifications].count("流程结束 · g") == 2


def test_needs_input_and_start_and_switches(paths, notifications):
    notify.needs_input(paths, "g", "t1", "用哪个分支？")
    notify.job_started(paths, "g", "goal")
    notify.job_started(paths, "retro-g-1", "复盘")          # 复盘 Job 是框架自动拉起的，不提醒
    assert notifications == [("需要人介入 · g", "t1: 用哪个分支？", "request"),
                             ("流程开始 · g", "goal", "none")]
    (paths.root / "team.yaml").write_text("agents: []\nnotify:\n  enabled: false\n", encoding="utf-8")
    notify.needs_input(paths, "g", "t1", "x")
    assert len(notifications) == 2
