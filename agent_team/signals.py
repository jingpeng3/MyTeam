"""任务信号：协调者写 inbox，worker 写 outbox；两端只认结构化 JSON。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .config import Paths, atomic_write_json, read_json_or
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


_FALSE_WORDS = {"false", "0", "no"}
_TRUE_WORDS = {"true", "1", "yes"}


def _as_str_list(v: Any) -> list[str]:
    """outbox 列表字段归一化：null→[]、单个串→[串]（防 list("/a.md") 拆成字符）、
    list/tuple 逐项转 str 并丢空项、其余类型→[]。"""
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return []


def _as_retryable(v: Any) -> bool | None:
    """retryable 归一化：bool/None 原样；"false"/"0"/"no" 等串（大小写不敏感）映射成 bool，
    否则 `res.retryable is False` 永不命中；无法识别的值按「未声明」None 处理。"""
    if v is None or isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _FALSE_WORDS:
        return False
    if s in _TRUE_WORDS:
        return True
    return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)  # tmp + os.replace，避免 worker 读到半截


def signal_filename(goal_id: str, task_id: str) -> str:
    """按 Job 命名空间隔离信号文件，避免同一 agent 服务多 Job 时撞车。"""
    return f"{goal_id}__{task_id}.json"


def _embedded_signal(raw: str) -> dict[str, Any] | None:
    """从「JSON 外裹散文/围栏」里救回信号对象：在每个 `{` 处试 raw_decode，取**含 status 的最长**对象。

    - 逐点 raw_decode 而非手写配平扫描：散文里的杂散 `"`（如 `5" 屏幕`）会翻转手写扫描的串内
      状态、令其后真信号整段被吞；raw_decode 每个起点独立解析，不受前文干扰。集合记号 `{50}`、
      模板占位 `{x}` 这类杂散花括号同样只是解析失败的起点，跳过即可。
    - 必须含 `status`：半截写入（worker 还在写）的外层 `{` 永不闭合，唯一完整的只剩嵌套对象
      （如 `"meta":{"files":3}`）。若照单全收，`_normalize_status` 会把它判 failed、白烧一次重试；
      拒收则返回 None → malformed → 走 idle 宽限，等 worker 写完。
    - 最长者优先：最外层/最完整的对象最可能是真信号；命中后跳过其内部起点（嵌套对象只会更短）。
    """
    dec = json.JSONDecoder()
    best: dict[str, Any] | None = None
    best_len = 0
    i = raw.find("{")
    while i != -1:
        try:
            d, end = dec.raw_decode(raw, i)
        except ValueError:          # JSONDecodeError 是其子类
            end = i + 1
        else:
            if isinstance(d, dict) and "status" in d:
                if end - i > best_len:
                    best, best_len = d, end - i
            else:
                end = i + 1          # 不合格的对象可能内含合格者（如外层缺 status），继续往里找
        i = raw.find("{", end)
    return best


def salvage_json(raw: str) -> dict[str, Any] | None:
    """worker 是 LLM，常把 JSON 包进 ```json 围栏或前后夹带解释文字，令 json.load 直接失败。
    读侧做一次宽容打捞：原样整段能解析出 dict 即采纳；否则从文中打捞含 status 的对象
    （见 _embedded_signal，天然覆盖围栏）。全失败才真判 malformed。"""
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if isinstance(d, dict):
        return d
    d = _embedded_signal(raw)
    if d is not None:   # 非原样命中：worker 把 JSON 裹进了围栏/散文，打捞救回，留痕便于观察
        log.debug("salvage_json 从围栏/散文中打捞出信号对象")
    return d


def write_inbox(paths: Paths, agent: str, goal_id: str,
                signal: dict[str, Any]) -> Path:
    p = paths.inbox(agent) / signal_filename(goal_id, signal["task_id"])
    _write_json(p, signal)
    return p


def _is_same_task_inbox(f: Path, task_id: str) -> bool:
    """inbox 文件 f 是否是 task_id 的在途信号（不论属于哪个 goal）。

    task_id 允许含 `__`，只比文件名后缀会误判：`job1__x__fetch.json`（task=x__fetch）也以
    `__fetch.json` 结尾。故优先读信号体里的 `task_id` 字段精确比较（to_signal 必写此字段；
    信号体不含 goal_id，goal 由规范文件名 `<goal>__<task>.json` 给出）。读不出 / 半截写入 /
    字段缺失时按文件名后缀回退——宁可多判一次「归属不明」，也不能把别人的交付吞掉。
    """
    if not f.name.endswith(f"__{task_id}.json"):
        return False   # 规范名必以 __<task_id>.json 结尾；不满足者不可能是本 task 的信号
    d = read_json_or(f, None)
    tid = d.get("task_id") if isinstance(d, dict) else None
    if isinstance(tid, str) and tid:
        return tid == task_id
    return True        # 回退：文件名后缀已命中，保守视为同 task


def _other_inflight_goals(indir: Path, goal_id: str, task_id: str) -> list[str]:
    """inbox 里同 task_id、但属于**别的** goal 的在途信号文件名（inbox 信号在任务在途期间一直存在）。

    逐项比对而非 glob：task_id 里若含 `[`/`*` 等字符，glob 会误匹配。
    """
    if not indir.is_dir():
        return []
    own = signal_filename(goal_id, task_id)
    try:
        return sorted(f.name for f in indir.iterdir()
                      if f.name != own and f.is_file() and _is_same_task_inbox(f, task_id))
    except OSError:
        return []


def _quarantine_ambiguous(alt: Path, goal_id: str, others: list[str]) -> None:
    """归属不明的裸名交付改名隔离：不再被任何一方当活信号读到，也保留原始字节供取证。

    只 return None 不够：一旦另一方被判交付缺失、reclaim 清掉了它的 inbox，本方下一轮就
    「看不到别人在途」而照常采纳——竞态窗口只是缩小、并未关上。隔离后两边都按交付缺失
    走原有重试流程。改名失败（竞态/权限）也绝不采纳，下一轮再试。
    """
    dest = alt.with_suffix(f".ambiguous.{int(time.time())}.json")
    try:
        alt.rename(dest)
    except OSError:
        log.warning("outbox 裸名信号 %s 归属不明，隔离改名失败、本轮不采纳（goal=%s 另有在途 %s）",
                    alt.name, goal_id, others)
        return
    log.warning("outbox 裸名信号归属不明（inbox 另有同 task 在途信号 %s），已隔离取证：%s（goal=%s）",
                others, dest.name, goal_id)


def _adopt_bare_outbox(outdir: Path, goal_id: str, task_id: str,
                       since: float | None, indir: Path | None = None) -> Path | None:
    """宽容采纳「名字写错」的交付：worker 偶发把信号写成裸 `<task_id>.json`。

    agent persona 模板历史上教的正是这个裸名（已修），故这类错名会零星复现；而产物本身
    往往已完整落盘，机械判 missing 会白重试一轮（x-ai-1015 就这样连挂两次判 FAILED）。
    采纳有两条硬约束，缺一不可——outbox 目录里真的躺着同名历史任务的陈旧信号（Oct 9/12 的
    裸名残留），无条件回退会把**旧交付**当成本次的：

      1) 必须知道本次派发时刻 `since`，且该文件 mtime 晚于它（早于派发 = 历史残留，不认）；
      2) 采纳即**改名为规范名**后返回：既让 clear_task_signals 能正常回收（否则留尸），
         也把「目录里裸名文件越积越多」的问题一并自愈。

    另有归属约束：裸名不带 goal，同一模板拉出的多个 Job（`plan X --as job1/job2`）在同一
    agent 上并发跑同名 task 时，无法判断这份裸名交付属于谁。若 inbox（`indir`）里还有**别的**
    goal 的同 task_id 在途信号，归属不明 → 隔离改名为 `<task_id>.ambiguous.<ts>.json`、不采纳
    （两边都按交付缺失走重试）；否则 job2 会吞掉 job1 的交付、误判 DONE 并回收在跑的 pane，
    job1 反被判交付缺失而重试。

    采纳时改名失败（竞态/权限）不阻断——仍返回裸名路径让调用方读到内容。
    """
    alt = outdir / f"{task_id}.json"
    if since is None or not alt.exists():
        return None
    try:
        if alt.stat().st_mtime < since:
            log.warning("outbox 存在同名裸名信号但早于本次派发，判为历史残留、不采纳：%s", alt)
            return None
    except OSError:
        return None
    others = _other_inflight_goals(indir, goal_id, task_id) if indir is not None else []
    if others:
        _quarantine_ambiguous(alt, goal_id, others)
        return None
    try:
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
    - "malformed"：文件存在但**打捞后仍**无法解析出信号对象（半截写入 / 输出契约坏）。
      读侧先经 salvage_json 宽容打捞（从围栏/散文中取含 status 的对象），扛住 LLM 最常见的
      「合法 JSON 外裹了 ```围栏/散文」破坏——这类此前会被误判 malformed → 成功跑成假失败。
    - "ok"       ：正常解析出 Result。

    文件名侧同样宽容：规范名不存在时，按 `since`（本次派发时刻）有界地采纳裸名信号
    （见 _adopt_bare_outbox）——内容容错与文件名容错对齐，不再一个极宽一个零容忍。
    """
    p = paths.outbox(agent) / signal_filename(goal_id, task_id)
    if not p.exists():
        adopted = _adopt_bare_outbox(paths.outbox(agent), goal_id, task_id, since,
                                     indir=paths.inbox(agent))
        if adopted is None:
            return None, "missing"
        p = adopted
    try:
        text = p.read_text(encoding="utf-8")
        written_at: float | None = p.stat().st_mtime   # worker 落盘时刻，比 monitor 观测到更准
    except (OSError, UnicodeDecodeError):
        # 半截写入可能切断中文的多字节字符 → 解码失败，与不可解析内容同判 malformed（走 idle 宽限）
        return None, "malformed"
    d = salvage_json(text)
    if d is None:
        return None, "malformed"   # 打捞也救不回；调用方按「已 idle + 超宽限」才终态判定
    # 字段类型逐个归一化：worker 是 LLM，null/错类型不能让 monitor_once 每轮抛异常
    err = d.get("error")
    return Result(
        task_id=str(d.get("task_id") or task_id),
        status=_normalize_status(d.get("status")),
        summary=str(d.get("summary") or ""),
        artifacts=_as_str_list(d.get("artifacts")),
        error=None if err is None else str(err),   # 保留 None＝未报错（board 按 is None 判断）
        retryable=_as_retryable(d.get("retryable")),   # 未声明则 None；false 表示永久失败、勿重试
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
