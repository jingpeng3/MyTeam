"""dashboard 读 run.log 尾部事件：只读文件尾、结果与整读实现一致。"""

from __future__ import annotations

import json

from agent_team.config import Paths
from agent_team.dashboard import _tail_events


def _paths(tmp_path) -> Paths:
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    p = Paths(tmp_path)
    p.state_dir.mkdir(parents=True, exist_ok=True)
    return p


def _naive(f, limit):
    """旧实现：整读后取末 limit 行，坏行跳过——作为对照基准。"""
    out = []
    for line in f.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_tail_large_log_with_truncated_last_line(tmp_path):
    p = _paths(tmp_path)
    f = p.state_dir / "run.log"
    body = "".join(json.dumps({"i": i, "msg": f"事件-{i}"}, ensure_ascii=False) + "\n"
                   for i in range(20000))
    f.write_text(body + '{"i": 20000, "msg": "写了一半', encoding="utf-8")   # 末行未写完
    got = _tail_events(p, 60)
    # 截断的末行算进 60 行窗口但解析失败被跳过 → 实得最后 59 条完整事件
    assert got == _naive(f, 60)
    assert [e["i"] for e in got] == list(range(19941, 20000))


def test_tail_small_log(tmp_path):
    p = _paths(tmp_path)
    f = p.state_dir / "run.log"
    f.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(10)) + "not json\n",
                 encoding="utf-8")
    assert _tail_events(p, 60) == [{"i": i} for i in range(10)]


def test_tail_lines_longer_than_window(tmp_path):
    p = _paths(tmp_path)
    f = p.state_dir / "run.log"
    pad = "x" * 100_000                                  # 单行 > 64KB，迫使窗口翻倍
    f.write_text("".join(json.dumps({"i": i, "pad": pad}) + "\n" for i in range(12)),
                 encoding="utf-8")
    got = _tail_events(p, 5)
    assert [e["i"] for e in got] == [7, 8, 9, 10, 11]
    assert got == _naive(f, 5)


def test_tail_missing_file(tmp_path):
    assert _tail_events(_paths(tmp_path)) == []
