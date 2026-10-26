"""扫 worker pane 尾部输出：识别「活着但在死循环报错」的致命信号与「停下等人拍板」的提问。

fatal_reason / awaiting_reason 是纯文本启发式（只吃 pane 文本、可单测）；读 pane 由 read_tail 负责。
"""
from __future__ import annotations

import re

from . import herdr


# 扫 worker pane 判致命/停滞信号时读取的最近输出行数。
PANE_SCAN_LINES = 40
# "attempt N/N" 判「重试耗尽」的最小 N：低于此不足以认定 agent 在原地打转。
RETRY_EXHAUSTED_MIN = 3
# 「活着但在死循环报错」的致命/停滞信号：herdr 会把正在重试报错的 agent 报成 working，
# 心跳分支于是无限刷新存活、idle 超时永不触发，只有 45min 绝对上限能兜——太慢。
# monitor 扫 worker pane 最近输出命中下列信号即判**终态失败、不重试**（重分派只会撞同一堵墙），
# 让 auth/密级/额度/重试耗尽这类流程问题在一个 monitor 周期内被识别，而不是空等很久。
_FATAL_PANE_SIGNATURES = [
    ("未被授权", "代码库未授权访问外部模型（密级）"),
    ("未授权访问", "未授权访问外部模型"),
    ("调整代码库密级", "代码库密级限制，模型访问被拒"),
    ("insufficient_quota", "模型额度耗尽"),
    ("额度已用尽", "模型额度耗尽"),
    ("额度不足", "模型额度不足"),
    ("rate_limit_exceeded", "被限流"),
    # 注：曾有裸 "permission denied" 签名——它在良性 shell 错误(EACCES)、甚至 worker 讨论文本里极常见，
    # 误报会把可自愈任务终态失败且不重试。已移除；密级/额度/限流这类**模型后端**专有串才保留。
]
# 终态重试耗尽：形如 "attempt 10/10"（两数相等且 ≥3）说明 agent 自身重试已到顶、在原地打转。
# 但纯 "5/5" 也可能出现在成功语境（"attempt 5/5 succeeded"），故要求附近有失败/放弃类语境词才判致命。
_RETRY_EXHAUSTED = re.compile(r"attempt\s+(\d+)\s*/\s*(\d+)")
_RETRY_FAIL_CONTEXT = re.compile(
    r"fail|failed|failure|error|giving up|gave up|exhaust|无法|失败|放弃|重试.*(耗尽|失败)|均失败",
    re.IGNORECASE)


def read_tail(herdr_name: str, lines: int = PANE_SCAN_LINES, live: bool = False) -> str:
    """读 worker pane 最近 lines 行；无名/读失败返回空串（扫描判定一律按「无信号」处理）。
    live=True 表示已知 agent 在 working，直接读可见屏（见 herdr.read_agent）。"""
    if not herdr_name:
        return ""
    try:
        return herdr.read_agent(herdr_name, lines=lines, live=live) or ""
    except herdr.HerdrError:
        return ""


def fatal_reason(text: str) -> str | None:
    """pane 输出里命中致命/停滞信号则返回简短原因，否则 None。"""
    text = text or ""
    low = text.lower()
    for needle, reason in _FATAL_PANE_SIGNATURES:
        if needle in text or needle.lower() in low:
            return reason
    for m in _RETRY_EXHAUSTED.finditer(text):
        cur, total = int(m.group(1)), int(m.group(2))
        if cur == total and total >= RETRY_EXHAUSTED_MIN and _RETRY_FAIL_CONTEXT.search(text):
            return f"重试耗尽（attempt {cur}/{total}），agent 在原地打转"
    return None


# worker 停下等人拍板的信号。显式哨兵最可靠（引导语里约定 worker 确需人决定时打印它）；
# 自由文本启发式作兜底，扫 pane 尾部若干行的问句/等待语，命中即判「等人介入」而非「挂死」。
AWAIT_SENTINEL = "[[NEEDS-INPUT]]"
# 容错哨兵：允许 -/_/空格 互换、大小写、内层可选空白。真 LLM worker 常改写标记形态（下划线/空格等）。
_AWAIT_SENTINEL_RE = re.compile(r"\[\[\s*needs[\s_-]*input\s*\]\]", re.IGNORECASE)
_AWAIT_PHRASES = re.compile(
    r"请确认|请选择|请回答|请输入|请提供|请问|需要你(确认|决定|提供|回复)"
    r"|等待.{0,6}(确认|回复|输入|回答)|waiting for (your |human )?(input|reply|answer|confirm)"
    r"|\(y/n\)|\[y/n\]", re.IGNORECASE)
# 形如 <你的问题> 的尖括号模板占位符：这是引导语里的示例残留，不是 worker 的真实问题。
_PLACEHOLDER_Q_RE = re.compile(r"^<[^>]*>$")
# 真实 claude/herdr pane 末尾恒为 TUI chrome（提示符/分隔线/状态行/权限提示）——自由文本兜底须跳过，
# 否则末行永远扫到 chrome、兜底恒失效（漏报把等人 worker 当挂死）。
_PANE_CHROME_RE = re.compile(
    r"^[>›❯$#*•\s]*$|^[-─—=_]{3,}$|bypass permissions|shift\+tab|for agents|"
    r"用户：|模型：|额度：|上下文：|esc to interrupt|\? for shortcuts|ctrl\+",
    re.IGNORECASE)
_PANE_MARK_STRIP = " \t`'\"*•-›>⏺●○◦▪·"   # 行首项目符号/引用符/提示符，判定前剥掉


def awaiting_reason(text: str) -> str | None:
    """pane 输出是否表明 worker **停下来等人拍板**（而非挂死）。命中返回问题摘要，否则 None。

    调用方须先确认 herdr 状态属 herdr.READY_STATES（turn 已结束）——「等输入」的前提是 worker 停下了；
    working 中出现问号只是思考过程，不算等待。

    关键：**只认 worker 自己输出行行首的哨兵**。此前用 `rfind` 扫整段，会命中我们发给 worker 的
    引导语里那句「…可在终端打印一行 [[NEEDS-INPUT]] <你的问题>」的**回显**——于是没在等人（甚至已
    挂死/交付缺失）的 worker 被误判为 awaiting、park 满 awaiting 上限（默认 1h）白占容量。现要求哨兵
    出现在（剥去 ⏺/项目符号后的）行首、且其后问题非空且非 `<…>` 占位符，才认定为真实等待。"""
    if not text:
        return None
    raw_lines = text.splitlines()
    # 显式哨兵：自底向上找「行首哨兵」的 worker 输出行（排除引导语里的句中/回显哨兵）。
    for ln in reversed(raw_lines):
        s = ln.strip().lstrip(_PANE_MARK_STRIP).strip()
        m = _AWAIT_SENTINEL_RE.match(s)      # match：必须在（剥符号后的）行首
        if not m:
            continue
        q = s[m.end():].strip(" :：`-").strip()
        if not q or _PLACEHOLDER_Q_RE.match(q):
            continue                          # 空 / `<你的问题>` 占位符 → 是引导语回显，非真实等待
        return q[:200]
    # 自由文本兜底：从尾部**跳过已知 TUI chrome 行**，取最近一行 worker 有效输出判问句。
    tail = [s for ln in raw_lines
            if (s := ln.strip()) and not _PANE_CHROME_RE.search(s)]
    if not tail:
        return None
    cand = tail[-1].lstrip(_PANE_MARK_STRIP).strip()
    if _PLACEHOLDER_Q_RE.match(cand):
        return None
    if cand.endswith("?") or cand.endswith("？") or _AWAIT_PHRASES.search(cand):
        return cand[:200]
    return None
