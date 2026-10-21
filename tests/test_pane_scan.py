"""pane 输出扫描：致命信号与等人提问识别（纯文本启发式）。"""
from __future__ import annotations

import pytest

from agent_team import pane_scan


@pytest.mark.parametrize("text, want", [
    ("Error: insufficient_quota", "模型额度耗尽"),
    ("RATE_LIMIT_EXCEEDED retrying", "被限流"),
    ("attempt 5/5 failed, giving up", "重试耗尽（attempt 5/5），agent 在原地打转"),
    ("attempt 2/2 failed", None),                      # N 太小不足以判打转
    ("attempt 5/5 succeeded", None),                   # 无失败语境
    ("bash: permission denied", None),                 # 良性 shell 错误不算致命
    ("", None),
])
def test_fatal_reason(text, want):
    assert pane_scan.fatal_reason(text) == want


@pytest.mark.parametrize("text, want", [
    ("⏺ [[NEEDS-INPUT]] 用哪个分支？\n> \n? for shortcuts", "用哪个分支？"),
    ("* [[needs_input]]: 要不要删表", "要不要删表"),
    # 引导语回显（句中哨兵 / <占位符>）不算等待
    ("可在终端打印一行 `[[NEEDS-INPUT]] <你的问题>` 然后停下\n已完成", None),
    ("[[NEEDS-INPUT]] <你的问题>", None),
    ("分析完毕。\n请确认是否继续\n────\n> ", "请确认是否继续"),
    ("Proceed with deploy?\nesc to interrupt", "Proceed with deploy?"),
    ("写完 outbox。\n> ", None),
    ("", None),
])
def test_awaiting_reason(text, want):
    assert pane_scan.awaiting_reason(text) == want


def test_read_tail_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise pane_scan.herdr.HerdrError("gone")
    monkeypatch.setattr(pane_scan.herdr, "read_agent", boom)
    assert pane_scan.read_tail("h1") == "" and pane_scan.read_tail("") == ""
