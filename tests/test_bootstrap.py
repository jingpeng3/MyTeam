"""worker 引导语：通用交付协议由框架统一下发，agent prompt 不再各自抄写。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_team import bootstrap, pane_scan
from agent_team.config import Paths
from agent_team.models import AgentManifest, Task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path)


def _boot(paths, task: Task) -> str:
    m = AgentManifest(name="w", role="测试角色", skills=["s1"], dir=str(paths.agent_dir("w")))
    return bootstrap.build_bootstrap(paths, m, "g", task)


def test_bootstrap_carries_full_protocol(paths):
    text = _boot(paths, Task(task_id="t", goal="x"))
    assert text.startswith("你是 team 的 测试角色。可用 skill：s1。")
    assert str(paths.inbox("w") / "g__t.json") in text
    assert str(paths.outbox("w") / "g__t.json") in text
    assert "不要自造 `outbox/<task_id>.json`" in text           # 原名警告从各 prompt 收进框架
    assert "inherited_artifacts" in text and "rejections" in text   # 信号字段说明
    assert str(paths.task_artifacts_dir("g", "t")) in text
    assert pane_scan.AWAIT_SENTINEL in text
    assert "retryable=false" in text
    assert "${" not in text                                      # 模板槽全部替换
    assert "打回**权限" not in text and "第 2 次尝试" not in text   # 条件段默认不出现


def test_bootstrap_conditional_notes(paths):
    t = Task(task_id="t", goal="x", reject_to=["up"], attempts=1,
             rejections=[{"from": "rev", "reason": "字段缺失", "evidence": ["/e.md"]}])
    text = _boot(paths, t)
    assert "第 2 次尝试" in text
    assert "第1次[来自 rev]：字段缺失；证据文件：/e.md" in text
    assert "打回**权限" in text and "['up']" in text
    # 条件段落在协议主体之间（输出字段说明之后、工作目录说明之前）
    assert text.index("retryable=false") < text.index("第 2 次尝试") < text.index("工作目录：")


def test_agent_prompts_do_not_repeat_protocol():
    """通用完成协议只在 agent_team/worker_protocol.md 一处；agent prompt 里再抄就会与框架漂移。"""
    offenders = []
    for p in sorted((ROOT / "agents").glob("*/prompt.md")):
        s = p.read_text(encoding="utf-8")
        if re.search(r"^## 完成协议", s, re.M) or "引导语给出的 outbox" in s:
            offenders.append(p.parent.name)
    assert offenders == []


def test_new_agent_template_has_no_protocol(tmp_path):
    from agent_team import registry
    p = Paths(tmp_path)
    p.team_file.write_text("agents: []\n", encoding="utf-8")
    adir = registry.create_agent(p, "demo", role="演示")
    s = (adir / "prompt.md").read_text(encoding="utf-8")
    assert "演示" in s and "完成协议" not in s and "outbox" not in s
