"""实时看板：把 coordinator/state 下的活账本聚合成 JSON，配一个深蓝工作台前端轮询。

服务端只用 stdlib（http.server），不引 web 框架；唯一三方依赖是全框架共用的 PyYAML。默认只绑 127.0.0.1——
这是**无鉴权**的本机只读面板，绝不要绑 0.0.0.0 暴露到网络。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

import yaml

from . import config, coordinator, registry, herdr
from .board import Board
from .config import Paths, read_json_or
from .models import (Task, DONE, FAILED, SKIPPED, DISPATCHED, READY, PENDING, BYPASSED,
                     AWAITING_INPUT, AWAITING_EXPANSION, DRY_PANE, PLACEHOLDER_RE,
                     runtime_key, split_runtime_key)

log = logging.getLogger(__name__)

_HTML = Path(__file__).with_name("dashboard.html")
# 终端控制序列（颜色/光标等）——pane 输出内联到面板前先剥掉，避免 UI 里出现乱码 ESC 字符
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f]")

# --- worker pane 实时尾读：短 TTL 缓存 + 单锁 ---------------------------------
# 面板轮询(2.5s)绝不批量读所有 worker pane（那是 N 次 subprocess/轮），只有「当前展开、
# 正在运行」的那一个节点按自己的节奏(3s)拉一次；这里再叠一层 1.8s 缓存，让重叠请求/快速重选合并。
_TAIL_TTL = 1.8
_tail_cache: dict[tuple[str, int, str], tuple[float, str]] = {}
_tail_lock = threading.Lock()


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s or "")


def agent_tail(name: str, lines: int = 44, session: str = "") -> str:
    """读某个 worker pane 最近 lines 行、剥掉 ANSI 控制序列。带 TTL 缓存、读失败不抛。
    缓存键含 lines：底部面板(18行)与最大化视图(120行)对同一 pane 各自缓存，互不覆盖。

    `session`：worker 所在的 herdr session（取自分派时写入 runtime 账本的 session 字段）。dashboard
    是独立进程，常被从没有 HERDR_SESSION 的终端拉起，不显式带 session 就会落到 default session、
    对每个 worker 报 agent_not_found；缓存键含 session，避免同名跨 session 串味。"""
    now = time.time()
    key = (name, lines, session)
    with _tail_lock:
        hit = _tail_cache.get(key)
        if hit and now - hit[0] < _TAIL_TTL:
            return hit[1]
    try:
        raw = herdr.read_agent(name, lines=lines, session=session or None)
    except (herdr.HerdrError, OSError) as e:
        raw = f"[无法读取 pane] {e}"
    text = _strip_ansi(raw)
    with _tail_lock:
        _tail_cache[key] = (now, text)
    return text


def inflight_tails(paths: Paths, lines: int = 18) -> dict[str, str]:
    """一次性批量读全部在途(非 dry-run) worker pane 的近段输出，喂给底部「执行中 Worker」
    面板每张卡片各一段实时纪录。N 个 worker = N 次读，但都过 agent_tail 的 1.8s TTL 缓存，
    且 N 通常个位数（受各 agent max_concurrent 约束），一次 HTTP 拉全部、前端不再 N 次请求。"""
    rt = _runtime(paths)
    out: dict[str, str] = {}
    for v in rt.values():
        if v.get("pane_id") == DRY_PANE:
            continue
        name = v.get("herdr_name") or ""
        if name and name not in out:
            out[name] = agent_tail(name, lines, session=_entry_session(v))
    return out


def _entry_session(v: dict[str, Any]) -> str:
    """账本条目记录的 worker session。记了空串＝分派方当时没设 HERDR_SESSION，即 default session，
    须显式按 default 读——不能回退到 dashboard 自己的 HERDR_SESSION（两者常不同）。
    完全没有 session 字段的旧条目才回退到本进程环境（返回空串）。"""
    if "session" not in v:
        return ""
    return v.get("session") or herdr.DEFAULT_SESSION_NAME


def _session_for(paths: Paths, name: str) -> str:
    """按 herdr_name 从 runtime 账本反查 worker 所在的 session。单节点按需拉取(_serve_tail)只拿到
    name，没有账本上下文，故在此补一次反查，让跨进程读取也命中对的 session。"""
    for v in _runtime(paths).values():
        if (v.get("herdr_name") or "") == name:
            return _entry_session(v)
    return ""


def _artifact_target(artifacts_dir: Path, raw: str) -> Path | None:
    """把 ?p= 解析成绝对路径；落在产物根之外（路径穿越/越权）返回 None。解析失败抛 OSError/ValueError。"""
    root = artifacts_dir.resolve()
    target = Path(unquote(raw)).resolve()
    if target != root and root not in target.parents:
        return None
    return target


def _find_slots(obj: Any, out: set[str]) -> None:
    """递归收集对象里所有字符串中的 {{槽}} 名。"""
    if isinstance(obj, str):
        out.update(PLACEHOLDER_RE.findall(obj))
    elif isinstance(obj, list):
        for x in obj:
            _find_slots(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_slots(v, out)


def _templates(paths: Paths) -> list[dict[str, Any]]:
    """把 coordinator/plans/*.json 读成流程模板（蓝图）列表。纯只读、坏文件跳过。
    模板 = DAG 骨架 + {{槽}}；这里只暴露形状与所需槽，不落地任何 Job。"""
    d = paths.plans_dir
    if not d.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("跳过坏模板 %s: %s", f.name, e)
            continue
        if not isinstance(data, dict):
            continue
        # 只把「真正的 plan」当模板：必须有非空 tasks 列表。否则跳过——避免误放进 plans/ 根的
        # 数据文件（如 --vars-file 的 key→value 值文件）被当成一个「0 步骤空模板」列出来。
        # 约定：vars/数据文件放 coordinator/plans/vars/（本 glob 非递归，不会扫到），别放 plans/ 根。
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            log.debug("跳过非 plan JSON（无非空 tasks 列表）：%s", f.name)
            continue
        slots: set[str] = set()
        _find_slots(data, slots)
        tasks = []
        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            tasks.append({
                "task_id": t.get("task_id", ""),
                "needs": t.get("needs", []) or [],
                # 产物继承 / 打回目标：蓝图区分「流程走向线」与「数据依赖」、画打回回路
                "see_artifacts": t.get("see_artifacts", []) or [],
                "reject_to": t.get("reject_to", []) or [],
                "capabilities": t.get("capabilities", []) or [],
                "goal": (t.get("goal", "") or "")[:600],
                "expect": (t.get("expect", "") or "")[:400],
                "max_retries": t.get("max_retries", 0),
                # fan-out 模板节点：蓝图里画成「×N 动态展开」叠层节点（前端据此渲染）
                "fanout": t.get("fanout") if isinstance(t.get("fanout"), dict) else None,
            })
        out.append({
            "name": f.stem,                       # 文件名（不含 .json）是稳定的模板标识
            "goal_id": data.get("goal_id", f.stem),
            "goal": (data.get("goal", "") or "")[:400],
            "slots": sorted(slots),
            "tasks": tasks,
            "ntasks": len(tasks),
        })
    return out


_SKILL_BODY_MAX = 6000   # 弹框里 SKILL.md 正文预览上限，避免超大 skill 撑爆单次响应


def _skill_info(sdir: Path) -> dict[str, Any]:
    """读 <skill>/SKILL.md 的 frontmatter(name/description) 与正文预览。读不到/格式坏时如实降级。"""
    info: dict[str, Any] = {"dir": sdir.name, "name": sdir.name, "description": "",
                            "path": str(sdir), "body": "", "ok": False}
    md = sdir / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        info["error"] = "缺少 SKILL.md"
        return info
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        body = text[m.end():]
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        if isinstance(fm, dict):
            info["name"] = str(fm.get("name") or sdir.name)
            info["description"] = str(fm.get("description") or "").strip()
    info["body"] = body.strip()[:_SKILL_BODY_MAX]
    info["truncated"] = len(body.strip()) > _SKILL_BODY_MAX
    info["ok"] = True
    return info


def agent_detail(paths: Paths, name: str) -> dict[str, Any] | None:
    """单个 agent 的配置 + skill 明细（看板点 agent 弹框用）。只认名册里的 agent 名，防路径穿越。
    skill 三类：
      - installed：agents/<name>/.claude/skills/ 下实际落位的（worker 启动即可见）；
      - declared：agent.yaml `skills` 声明的，标出「声明未落位」；
      - hidden：团队默认 worker_hidden_skills ∪ agent hidden_skills（分派时对 worker 强制关闭）。"""
    if name not in config.list_agent_names(paths):
        return None
    m = config.load_manifest(paths, name)
    adir = Path(m.dir)
    sroot = adir / ".claude" / "skills"
    installed = ([_skill_info(d) for d in sorted(sroot.iterdir()) if d.is_dir()]
                 if sroot.is_dir() else [])
    have = {s["dir"] for s in installed} | {s["name"] for s in installed}
    team_hidden = list((config.load_team(paths).get("defaults") or {}).get("worker_hidden_skills") or [])
    return {
        "name": m.name, "role": m.role, "description": m.description,
        "capabilities": m.capabilities, "max_concurrent": m.max_concurrent,
        "lifecycle": m.lifecycle,
        "runtime": {"cli": m.runtime.cli, "kind": m.runtime.kind,
                    "model": m.runtime.model, "flags": m.runtime.flags},
        "dir": str(adir), "skills_dir": str(sroot),
        "declared": m.skills,
        "declared_missing": [s for s in m.skills if s not in have],
        "installed": installed,
        "hidden_team": team_hidden,
        "hidden_agent": m.hidden_skills,
    }


def _runtime(paths: Paths) -> dict[str, Any]:
    return read_json_or(paths.runtime_file, {})


def _tail_events(paths: Paths, limit: int = 60) -> list[dict[str, Any]]:
    """读 run.log(JSONL) 末尾若干条事件，解析失败的行跳过。

    run.log 只追加不轮转，只从文件尾按窗口(起步 64KB)倒读：窗口内凑不够 limit 行就翻倍重读，
    直到够数或读到文件头——轮询开销与日志总长无关。"""
    f = paths.state_dir / "run.log"
    if limit <= 0:
        return []
    window = 64 * 1024
    try:
        with open(f, "rb") as fh:
            size = fh.seek(0, 2)
            while True:
                offset = max(0, size - window)
                fh.seek(offset)
                data = fh.read(size - offset)
                if offset > 0:                       # 窗口起点多半落在行中间，丢掉这截残行
                    data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
                lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines()
                         if ln.strip()]
                if offset == 0 or len(lines) >= limit:
                    break
                window *= 2
    except OSError:                                  # 含文件不存在
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _elapsed(t: Task, entry: dict[str, Any], now: float) -> float | None:
    """在跑任务已运行时长：优先取 board 运行记录的开始时刻——账本 dispatched_at 会在等人恢复/答复时
    被重置（防存活上限误杀），拿它算会把「已运行」清零。旧 board 无运行记录才退回账本。"""
    if t.status != DISPATCHED:
        return None
    run = t.current_run()
    started = run["started_at"] if run else entry.get("dispatched_at")
    return round(now - started, 1) if started else None


def _run_view(r: dict[str, Any], now: float) -> dict[str, Any]:
    """运行记录给前端的精简视图（在开的按 now 现算）。"""
    v = Task.run_live(r, now)
    return {k: v.get(k) for k in ("attempt", "agent", "started_at", "ended_at", "end_source",
                                  "queue_s", "provision_s", "wall_s", "active_s", "awaiting_s",
                                  "outcome")}


def _job_summary(board: Board, rt: dict[str, Any], now: float) -> dict[str, Any]:
    """轻量 Job 摘要：喂给 2.5s 轮询的左栏列表。**不含任务重字段**（goal/summary/error/
    expect/needs/rejections/artifacts/context_refs）——那些只在点开 DAG 时经 /api/job 按需拉。
    保留卡片 chip 流水线与徽标需要的最小任务桩，payload 因此几乎不随 Job 数膨胀。"""
    counts = Counter(t.status for t in board.tasks.values())
    tasks = []
    for t in board.tasks.values():
        entry = rt.get(runtime_key(board.goal_id, t.task_id), {})
        elapsed = _elapsed(t, entry, now)
        tm = t.timing(now)
        tasks.append({
            "task_id": t.task_id,
            "status": t.status,
            "agent": t.agent,
            "capabilities": t.capabilities,          # chip tooltip 用（短数组，保留）
            "reject_count": t.reject_count,           # jobRejects / chip ↩N 徽标
            "elapsed_s": elapsed,
            "active_s": tm["active_s"],                # chip 上的完成耗时（最近一次运行的净执行）
            "runs": tm["runs"],
            "escalated": bool(entry.get("escalated")),  # jobEscalated 徽标
            # 「需要人决策」徽标：worker 停下等人拍板时，把它正在问的问题随摘要一并带出（截断、
            # 仅 awaiting_input 时非空），供 jobAwaiting 的 Job 卡徽标 tooltip 一眼看到待决内容。
            "awaiting_reason": (entry.get("awaiting_reason") or "")[:160] if t.status == AWAITING_INPUT else "",
            "fanout": bool(t.fanout),                 # .fan chip class + fanb 徽标（只需布尔）
            "fanout_group": t.fanout_group,
        })
    return {
        "goal_id": board.goal_id,
        "goal": (board.goal or "")[:200],
        "complete": board.is_complete(),
        "ok": board.is_complete() and not board.has_failures(),
        "has_failures": board.has_failures(),
        "summary": board.summary(),
        "counts": dict(counts),
        "tasks": tasks,
    }


def _job_detail(board: Board, rt: dict[str, Any], now: float) -> dict[str, Any]:
    counts = Counter(t.status for t in board.tasks.values())
    tasks = []
    for t in board.tasks.values():
        entry = rt.get(runtime_key(board.goal_id, t.task_id), {})
        elapsed = _elapsed(t, entry, now)
        tm = t.timing(now)
        tasks.append({
            "task_id": t.task_id,
            "status": t.status,
            "agent": t.agent,
            "needs": t.needs,
            "see_artifacts": t.see_artifacts,   # 继承哪些上游产物：流程图把跨层的数据依赖与流程线分开画
            "goal": (t.goal or "")[:600],     # 节点当初被要求做什么：排查 retask/打回后指令是否生效的一手信息
            "attempts": t.attempts,
            "max_retries": t.max_retries,
            "capabilities": t.capabilities,
            "summary": (t.result_summary or "")[:280],
            "error": (t.error or "")[:280],
            "elapsed_s": elapsed,
            "timing": tm,                                          # 最近一次运行的排队/净执行/等人 + 累计
            "runs": [_run_view(r, now) for r in t.runs[-20:]],    # 每次尝试明细（截尾防 payload 膨胀）
            "pane": entry.get("herdr_name") or entry.get("pane_id") or "",
            "herdr_name": entry.get("herdr_name") or "",
            "artifacts": t.artifacts,
            "expect": (t.expect or "")[:400],   # 与 _templates 对齐截断，避免超长字段撑爆每轮 payload
            "context_refs": t.context_refs,
            # 打回（reject/send-back）相关：供流程图打回边/徽标 + 全局看板「打回记录」
            "reject_to": t.reject_to,
            "reject_count": t.reject_count,
            "rejections": t.rejections,
            "max_rejects": t.max_rejects,
            # fan-out：模板节点(fanout 非空)画成叠层 + AWAITING_EXPANSION 提示待展开；
            # 子任务(fanout_group 非空)可按组框起、显示「在跑 x/上限」。
            "fanout": t.fanout,
            "fanout_group": t.fanout_group,
            "fanout_max_parallel": t.fanout_max_parallel,
            "escalated": bool(entry.get("escalated")),        # 打回超限、停在此节点等协调者裁决
            "awaiting_reason": entry.get("awaiting_reason", ""),
        })
    return {
        "goal_id": board.goal_id,
        "goal": board.goal,
        "version": board.version,
        "timing": coordinator.job_timing(board, now),
        "agent_timing": coordinator.timing_by_agent(board, now),
        "complete": board.is_complete(),
        "ok": board.is_complete() and not board.has_failures(),
        "has_failures": board.has_failures(),
        "summary": board.summary(),
        "counts": dict(counts),
        "tasks": tasks,
    }


def build_state(paths: Paths) -> dict[str, Any]:
    """把所有 Job / agent / 在途 pane / 近期事件聚合成一份看板快照。纯只读，无副作用。"""
    now = time.time()
    boards = Board.load_all(paths)
    rt = _runtime(paths)
    # 每个 agent 当前占用的真实 pane 数（排除 dry-run 幽灵）
    load = Counter(v.get("agent") for v in rt.values()
                   if v.get("agent") and v.get("pane_id") != DRY_PANE)

    agents = []
    for m in registry.list_agents(paths):
        agents.append({
            "name": m.name,
            "role": m.role,
            "capabilities": m.capabilities,
            "skills": m.skills,
            "max_concurrent": m.max_concurrent,
            "load": load.get(m.name, 0),
        })

    inflight = []
    for key, v in rt.items():
        if v.get("pane_id") == DRY_PANE:
            continue
        if ":" not in key:                      # 畸形 runtime key（无复合分隔）：跳过而非产出 gid==tid 的幽灵在途行
            continue
        started = v.get("dispatched_at")
        kgid, ktid = split_runtime_key(key)
        inflight.append({
            "goal_id": v.get("goal_id", kgid),
            "task_id": v.get("task_id", ktid),
            "agent": v.get("agent", ""),
            "pane": v.get("herdr_name") or v.get("pane_id") or "",
            "herdr_name": v.get("herdr_name") or "",
            "elapsed_s": round(now - started, 1) if started else None,
            "idle": bool(v.get("idle_since")),
        })
    inflight.sort(key=lambda x: (x["elapsed_s"] is None, -(x["elapsed_s"] or 0)))

    jobs = [_job_summary(b, rt, now) for b in boards]
    for j in jobs:                              # 账本文件 mtime 作为「最近活动时间」，用于从新到旧排序
        try:
            j["mtime"] = paths.job_file(j["goal_id"]).stat().st_mtime
        except OSError:
            j["mtime"] = 0.0
    jobs.sort(key=lambda j: j["mtime"], reverse=True)   # 从新到旧：刚完成/刚动过的 Job 置顶，好找

    # 模板瘦身：轮询只回模板骨架（name/goal/slots/ntasks + 每步 {task_id,needs,fanout}，
    # 够卡片 chip 流水线渲染）；完整 goal/expect 明细在点开模板 DAG 时经 /api/job?template= 拉。
    templates = [{
        "name": t["name"], "goal_id": t["goal_id"], "goal": (t["goal"] or "")[:200],
        "slots": t["slots"], "ntasks": t["ntasks"],
        "tasks": [{"task_id": x["task_id"], "needs": x["needs"],
                   "fanout": bool(x.get("fanout"))} for x in t["tasks"]],
    } for t in _templates(paths)]

    agg = Counter()
    for j in jobs:
        for k, v in j["counts"].items():
            agg[k] += v
    # 打回聚合：跨所有 Job 的累计被打回次数 + 当前处于「升级待裁决」的节点数
    rejects = sum(t.get("reject_count", 0) for j in jobs for t in j["tasks"])
    escalations = sum(1 for j in jobs for t in j["tasks"] if t.get("escalated"))
    # 「执行中」应反映真实在跑 pane：dry-run 幽灵的 board 任务也是 DISPATCHED，但没有真 worker
    # （inflight/agent load 已排除它），故从 running 里一并剔除，避免「执行中 N 但工作台为空」的误导。
    dry_dispatched = sum(1 for v in rt.values() if v.get("pane_id") == DRY_PANE)
    stats = {
        "jobs_total": len(jobs),
        "jobs_active": sum(1 for j in jobs if not j["complete"]),
        "jobs_ok": sum(1 for j in jobs if j["ok"]),
        "jobs_failed": sum(1 for j in jobs if j["complete"] and j["has_failures"]),
        "tasks_total": sum(agg.values()),
        "done": agg.get(DONE, 0),
        "failed": agg.get(FAILED, 0),
        "skipped": agg.get(SKIPPED, 0),
        "bypassed": agg.get(BYPASSED, 0),
        "running": max(0, agg.get(DISPATCHED, 0) - dry_dispatched),
        "ready": agg.get(READY, 0),
        "pending": agg.get(PENDING, 0),
        "awaiting_expansion": agg.get(AWAITING_EXPANSION, 0),   # fan-out 模板待协调者展开
        # 「需要人决策」总数：所有 AWAITING_INPUT 任务（worker 停下等人答复 + 打回触顶升级待裁决）。
        # escalations 是其中「打回触顶」的子集，二者都要人介入才能继续。
        "awaiting_input": agg.get(AWAITING_INPUT, 0),
        "rejects": rejects,
        "escalations": escalations,
        "agents": len(agents),
        "inflight": len(inflight),
        "templates": len(templates),
    }
    return {"ts": now, "stats": stats, "jobs": jobs, "templates": templates,
            "agents": agents, "inflight": inflight, "events": _tail_events(paths)}


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# /file 产物响应头：产物是 worker（LLM）写的，html/svg 若按原样内联，脚本会跑在看板同源下，
# 能 fetch /api/state、/api/agent_tail、其它 /file —— 即存储型 XSS。CSP sandbox 把文档放进
# 不透明源（不给 allow-same-origin），脚本仍可执行以便预览交互页面，但读不到看板任何接口；
# nosniff 防浏览器把 text/plain 等内容嗅探成 html 执行。
_ARTIFACT_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts",
    "X-Content-Type-Options": "nosniff",
}


def _host_name(host_header: str | None) -> str:
    """从 Host 头取主机名：去端口、去 IPv6 方括号、转小写。缺失/空 → ""。
    `localhost:8787`→`localhost`；`[::1]:8787`→`::1`；裸 `::1`（多冒号无括号）原样。"""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h


def _make_handler(paths: Paths, host: str = "127.0.0.1"):
    # 绑回环地址时校验 Host 头防 DNS rebinding：恶意网页把自家域名解析到 127.0.0.1 后，
    # 浏览器会带着 `Host: evil.example` 来读本机面板。缺 Host 也拒（浏览器/HTTP1.1 必带，
    # 无法核验就不放行）。绑非回环地址是用户显式选择且已告警，不强制（否则按 IP/域名访问会被误拒）。
    check_host = host in _LOOPBACK_HOSTS

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # 静音默认 stderr 访问日志
            return

        def _send(self, code: int, body: bytes, ctype: str,
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _qs(self, name: str, default: str = "") -> str:
            return (parse_qs(urlparse(self.path).query).get(name) or [default])[0].strip()

        def do_GET(self):
            if check_host and _host_name(self.headers.get("Host")) not in _LOOPBACK_HOSTS:
                self._send(403, b"forbidden (bad Host header)", "text/plain")
                return
            # 按路径精确路由（不做前缀匹配：否则 /api/job 会吞掉 /api/jobs 之类的新路由）
            route = {
                "/api/state": self._serve_state,
                "/api/job": self._serve_job,
                "/api/agent": self._serve_agent,
                "/api/agent_tail": self._serve_tail,
                "/api/inflight_tails": self._serve_inflight_tails,
                "/file": self._serve_artifact,
                "/": self._serve_index,
                "/index.html": self._serve_index,
            }.get(urlparse(self.path).path)
            if route is None:
                self._send(404, b"not found", "text/plain")
            else:
                route()

        def _serve_index(self) -> None:
            try:
                html = _HTML.read_bytes()
            except OSError as e:
                self._send(500, f"dashboard.html missing: {e}".encode(), "text/plain")
                return
            self._send(200, html, "text/html; charset=utf-8")

        def _serve_state(self) -> None:
            try:
                self._send_json(build_state(paths))
            except Exception as e:                           # noqa: BLE001
                log.warning("build_state 失败: %s", e)
                self._send_json({"error": str(e)})

        def _serve_job(self) -> None:
            """按需返回单个 Job / 模板的**完整明细**（列表/详情拆分的详情侧）。
            仅在点开某个 DAG 覆盖层时拉一次、并随 tick 刷新——不进 2.5s 批量轮询，
            故轮询 payload 不随 Job 数膨胀。
              ?goal_id=<id>  → 该 Job 的全量任务明细（_job_detail）
              ?template=<name> → 该模板的完整条目（含每步 goal/expect）"""
            gid, tname = self._qs("goal_id"), self._qs("template")
            try:
                if gid:
                    board = Board.load(paths, gid)
                    out = (_job_detail(board, _runtime(paths), time.time()) if board is not None
                           else {"error": f"job not found: {gid}"})
                elif tname:
                    hit = next((t for t in _templates(paths) if t["name"] == tname), None)
                    out = hit or {"error": f"template not found: {tname}"}
                else:
                    out = {"error": "missing goal_id or template"}
            except Exception as e:                           # noqa: BLE001
                log.warning("_serve_job 失败: %s", e)
                out = {"error": str(e)}
            self._send_json(out)

        def _serve_agent(self) -> None:
            """?name=<agent> → 该 agent 的配置与 skill 明细（点 agent 弹框时按需拉一次，不进轮询）。"""
            name = self._qs("name")
            try:
                d = agent_detail(paths, name) if name else None
                out = d or {"error": f"agent not found: {name}"}
            except Exception as e:                           # noqa: BLE001
                log.warning("_serve_agent 失败: %s", e)
                out = {"error": str(e)}
            self._send_json(out)

        def _serve_tail(self) -> None:
            """按 ?name=<herdr_name>&lines=N 内联返回某个在跑 worker 的近段对话（纯文本）。
            仅供「展开中且运行中」的单节点按需拉取——不进 /api/state 批量轮询，避免每轮 N 次 subprocess。"""
            name = self._qs("name")
            if not name:
                self._send_json({"error": "missing name"}, 400)
                return
            try:
                n = max(8, min(80, int(self._qs("lines", "44"))))
            except ValueError:
                n = 44
            text = agent_tail(name, n, session=_session_for(paths, name))
            self._send_json({"name": name, "text": text})

        def _serve_inflight_tails(self) -> None:
            """?lines=N 一次返回全部在途 worker 的近段输出 {name: text}。底部执行中面板每卡一段。"""
            try:
                n = max(6, min(60, int(self._qs("lines", "18"))))
            except ValueError:
                n = 18
            self._send_json({"tails": inflight_tails(paths, n)})

        def _serve_artifact(self) -> None:
            """按 ?p= 提供产物文件下载/预览。**严格限定在 shared/artifacts/ 内**——
            面板无鉴权、只绑本机，绝不能变成任意文件读取，故解析后校验路径确在产物根下。"""
            raw = (parse_qs(urlparse(self.path).query).get("p") or [""])[0]   # 不 strip：路径原样
            if not raw:
                self._send(400, b"missing ?p=", "text/plain")
                return
            try:
                target = _artifact_target(paths.artifacts_dir, raw)
            except (OSError, ValueError):
                self._send(400, b"bad path", "text/plain")
                return
            if target is None:
                self._send(403, b"forbidden (outside artifacts root)", "text/plain")
                return
            if not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype, _ = mimetypes.guess_type(target.name)
            # md/txt/json/log 等按纯文本内联预览；html 内联；其余当附件下载。
            if target.suffix.lower() in (".md", ".txt", ".json", ".log", ".yaml", ".yml", ""):
                ctype = "text/plain; charset=utf-8"
            elif ctype in ("text/html", "application/xhtml+xml"):
                ctype = "text/html; charset=utf-8"
            try:
                body = target.read_bytes()
            except OSError as e:
                self._send(500, f"read failed: {e}".encode(), "text/plain")
                return
            self._send(200, body, ctype or "application/octet-stream", headers=_ARTIFACT_HEADERS)

    return Handler


def serve(paths: Paths, port: int = 8787, host: str = "127.0.0.1") -> None:
    if host not in _LOOPBACK_HOSTS:
        # 看板无鉴权、可读全部 Job/产物：绑非回环地址等于把它暴露到网络，醒目告警。
        log.warning("dashboard 绑定了非回环地址 %s —— 面板无鉴权，任何能访问该地址的人都能读全部 "
                    "Job/产物，仅在可信内网/明确知情时使用", host)
        print(f"⚠ 警告：--host {host} 非回环地址，看板无鉴权将暴露到网络！")
    httpd = ThreadingHTTPServer((host, port), _make_handler(paths, host=host))
    url = f"http://{host}:{port}"
    print(f"TEAMS dashboard → {url}  (Ctrl+C 停止；仅本机、无鉴权)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard 已停止")
    finally:
        httpd.server_close()
