"""任务信号：协调者写 inbox，worker 写 outbox；两端只认结构化 JSON。"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .config import Paths, atomic_write_json
from .models import Result, DONE, FAILED, REJECT

log = logging.getLogger(__name__)

# worker 用自然语言写 status 时的常见"成功"同义词——归一化到 DONE，避免真人/真 LLM
# worker 写 "completed"/"success"/"ok" 被精确串匹配判为失败而无谓重试。
_DONE_SYNONYMS = {"done", "success", "succeeded", "completed", "complete",
                  "ok", "okay", "passed", "finished"}
# 评审/校验节点「打回上游」的动词同义词——归一化到 REJECT（结果动词，非任务状态）。
# 与 DONE/FAILED 并列先行拦截，避免 "reject" 落到 _normalize_status 被当失败重试。
_REJECT_SYNONYMS = {"reject", "rejected", "sendback", "send_back", "send-back",
                    "打回", "退回", "驳回"}


def _normalize_status(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in _REJECT_SYNONYMS:
        return REJECT
    if s in _DONE_SYNONYMS:
        return DONE
    return FAILED  # 缺失/未知/显式失败一律判失败（仅成功/打回同义词才另归一）


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)  # tmp + os.replace，避免 worker 读到半截


def signal_filename(goal_id: str, task_id: str) -> str:
    """按 Job 命名空间隔离信号文件，避免同一 agent 服务多 Job 时撞车。"""
    return f"{goal_id}__{task_id}.json"


def _balanced_json_objects(raw: str):
    """产出 raw 中所有「花括号平衡」的子串（字符串/转义感知），**长者优先**。

    用于从「合法 JSON 外裹散文、且散文里还夹带杂散花括号」里救回对象——worker(LLM) 常在解释
    文字里写集合记号 `{50}`、模板占位 `{x}` 或示例 JSON 片段。此时朴素的 `find('{')..rfind('}')`
    首尾切片会把 `{50}. ... {真信号}` 连成一大段不可解析，反把成功跑误判为 malformed。改为扫描
    所有配平的 `{…}`、按长度降序逐个交给 json.loads——最外层/最完整的对象最可能是真信号。
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_str = esc = False
    for idx, ch in enumerate(raw):
        if in_str:                       # 串内的花括号不计深度，转义感知避免 \" 误判串尾
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(idx)
        elif ch == "}" and stack:
            spans.append((stack.pop(), idx + 1))
    for start, end in sorted(spans, key=lambda s: s[1] - s[0], reverse=True):
        yield raw[start:end]


def _salvage_candidates(raw: str):
    """按「最可能正确」到「兜底」的顺序，产出待解析的候选字符串。"""
    yield raw                                   # 1) 原样：worker 已产出合法 JSON 的正常路径
    stripped = raw.strip()
    if stripped.startswith("```"):              # 2) 剥 markdown 围栏：```json ... ``` / ``` ... ```
        body = re.sub(r"^```[^\n]*\n?", "", stripped)
        body = re.sub(r"\n?```\s*$", "", body)
        yield body
    # 3) 花括号平衡扫描：从「JSON 外裹散文（可能含杂散花括号）」里枚举配平对象、长者优先。
    yield from _balanced_json_objects(raw)


def salvage_json(raw: str) -> dict[str, Any] | None:
    """worker 是 LLM，常把 JSON 包进 ```json 围栏或前后夹带解释文字，令 json.load 直接失败。
    读侧做一次宽容打捞：原样 → 剥围栏 → 截最外层大括号块，任一能解析出 dict 即采纳；
    全失败才真判 malformed。只在解析已失败的分支触发，正常路径零开销、无行为改变。"""
    for idx, candidate in enumerate(_salvage_candidates(raw)):
        try:
            d = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict):
            if idx > 0:   # 非原样命中：worker 把 JSON 裹进了围栏/散文，打捞救回，留痕便于观察
                log.debug("salvage_json 通过候选 #%d 打捞成功（原始 JSON 被围栏/散文包裹）", idx)
            return d
    return None


def write_inbox(paths: Paths, agent: str, goal_id: str,
                signal: dict[str, Any]) -> Path:
    p = paths.inbox(agent) / signal_filename(goal_id, signal["task_id"])
    _write_json(p, signal)
    return p


def _adopt_bare_outbox(outdir: Path, goal_id: str, task_id: str,
                       since: float | None) -> Path | None:
    """宽容采纳「名字写错」的交付：worker 偶发把信号写成裸 `<task_id>.json`。

    agent persona 模板历史上教的正是这个裸名（已修），故这类错名会零星复现；而产物本身
    往往已完整落盘，机械判 missing 会白重试一轮（x-ai-1015 就这样连挂两次判 FAILED）。
    采纳有两条硬约束，缺一不可——outbox 目录里真的躺着同名历史任务的陈旧信号（Oct 9/12 的
    裸名残留），无条件回退会把**旧交付**当成本次的：

      1) 必须知道本次派发时刻 `since`，且该文件 mtime 晚于它（早于派发 = 历史残留，不认）；
      2) 采纳即**改名为规范名**后返回：既让 clear_task_signals 能正常回收（否则留尸），
         也把「目录里裸名文件越积越多」的问题一并自愈。

    改名失败（竞态/权限）不阻断——仍返回裸名路径让调用方读到内容。
    """
    alt = outdir / f"{task_id}.json"
    if since is None or not alt.exists():
        return None
    try:
        if alt.stat().st_mtime < since:
            log.warning("outbox 存在同名裸名信号但早于本次派发，判为历史残留、不采纳：%s", alt)
            return None
        dest = outdir / signal_filename(goal_id, task_id)
        alt.rename(dest)
        log.warning("outbox 信号名不规范（裸 task_id），已归位采纳：%s → %s", alt.name, dest.name)
        return dest
    except OSError:
        return alt


def read_outbox_ex(paths: Paths, agent: str, goal_id: str,
                   task_id: str, since: float | None = None) -> tuple[Result | None, str]:
    """读 outbox 并返回 (结果, 状态)。状态区分三种情形，供 monitor 精确处置：

    - "missing"  ：文件不存在（worker 尚未交付 / 静默死亡）。
    - "malformed"：文件存在但**打捞后仍**无法解析出 JSON 对象（半截写入 / 输出契约坏）。
      读侧先经 salvage_json 宽容打捞（剥围栏、截大括号块），扛住 LLM 最常见的
      「合法 JSON 外裹了 ```围栏/散文」破坏——这类此前会被误判 malformed → 成功跑成假失败。
    - "ok"       ：正常解析出 Result。

    文件名侧同样宽容：规范名不存在时，按 `since`（本次派发时刻）有界地采纳裸名信号
    （见 _adopt_bare_outbox）——内容容错与文件名容错对齐，不再一个极宽一个零容忍。
    """
    p = paths.outbox(agent) / signal_filename(goal_id, task_id)
    if not p.exists():
        adopted = _adopt_bare_outbox(paths.outbox(agent), goal_id, task_id, since)
        if adopted is None:
            return None, "missing"
        p = adopted
    try:
        text = p.read_text(encoding="utf-8")
        written_at: float | None = p.stat().st_mtime   # worker 落盘时刻，比 monitor 观测到更准
    except OSError:
        return None, "malformed"
    d = salvage_json(text)
    if d is None:
        return None, "malformed"   # 打捞也救不回；调用方按「已 idle + 超宽限」才终态判定
    return Result(
        task_id=d.get("task_id", task_id),
        status=_normalize_status(d.get("status")),
        summary=d.get("summary", ""),
        artifacts=list(d.get("artifacts", [])),
        error=d.get("error"),
        retryable=d.get("retryable"),   # 未声明则 None；worker 置 false 表示永久失败、勿重试
        reject_to=str(d.get("reject_to") or ""),   # status=reject 时：打回目标上游 task_id
        written_at=written_at,
    ), "ok"


def clear_task_signals(paths: Paths, agent: str, goal_id: str,
                       task_id: str, quarantine_outbox: bool = False) -> None:
    fname = signal_filename(goal_id, task_id)
    (paths.inbox(agent) / fname).unlink(missing_ok=True)
    outb = paths.outbox(agent) / fname
    if not outb.exists():
        return
    if quarantine_outbox:
        # 判 malformed 时别直接删——保留原始字节供事后取证（否则根因随手抹掉，只剩一句
        # "delivery corrupt"）。改名隔离，不再被后续 read_outbox 当活信号读到。
        dest = outb.with_suffix(f".malformed.{int(time.time())}.json")
        outb.rename(dest)
        log.warning("outbox 信号损坏，已隔离取证：%s（goal=%s task=%s）",
                    dest.name, goal_id, task_id)
    else:
        outb.unlink(missing_ok=True)
