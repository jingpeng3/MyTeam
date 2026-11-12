"""流程优化 campaign：回顾一个时间窗内的真实运行，按流程优化、独立评审、过门禁后合并、出 HTML 报告。

编排（派 subagent、判断修复轮次）由 `.claude/skills/team-optimize` 驱动；本模块只做确定性部分：
  collect  窗口内已收敛 Job → 归属 plan 模板 → 指标与问题信号 → 选流程、划改动边界（共享 agent 定 owner）、
           带出上次未闭环的跨流程请求（pending_cross_flow）
  bookmark 书签：已覆盖到哪天、每条流程最近一次合并的优化；collect 默认从书签续接，早于上次合并的 Job 不计分
  log      只追加的优化日志 optimize-log.jsonl；状态类事件同步更新 campaign.json
  finish   汇总 result.json、写入优化历史（入库，下次 collect 据此评估上次改动的效果）、渲染报告
worktree / 门禁 / 合并见 optimize_ops，HTML 渲染见 optimize_report。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import registry, retro
from .board import Board
from .config import Paths, atomic_write_json, file_lock, read_json_or
from .models import DONE

ADHOC = "_adhoc"             # 没匹配到 plan 模板的临时 Job：只进报告，不优化
MAX_FLOWS = 10               # 单次 campaign 最多优化的流程数
HOTSPOT_SHARE = 0.4          # 单步净执行占墙钟比例达到即报热点
HOTSPOT_MIN_WALL_S = 300     # 墙钟太短的 Job 不报热点
MAX_FIX_ROUNDS = 2           # 评审 FAIL 后最多修复几轮，超出只能放弃
FAIL_OUTCOMES = frozenset({"failed", "timeout", "fatal"})
# 只有流程类信号计入选流程得分；框架类信号单独汇总成框架问题候选
# contract_violation：收割时产物违反 plan 声明的契约（task_contract_violation）。monitor 每次违约都紧跟记一条
# task_failed（不论是否还有重试），失败已计 3 分，这里只按产物哨兵计 1 分（与 artifact_warning 同级），单次违约合计 4 分
SIGNAL_WEIGHTS = {"failure": 3, "lifetime_kill": 3, "timeout": 2, "reject": 2, "retask": 2,
                  "bypass": 1, "soft_error": 1, "artifact_warning": 1, "contract_violation": 1, "hotspot": 1}
FRAMEWORK_KINDS = {"provision_failure": "拉起 worker 失败",
                   "awaiting_reentry": "答复后重复进入等人",
                   "interrupt_misread": "中断提示被当成 worker 提问"}
SIGNAL_LABELS = {"failure": "失败", "lifetime_kill": "寿命上限回收", "timeout": "超时",
                 "reject": "打回", "retask": "人工改写 goal", "bypass": "人工跳过",
                 "soft_error": "完成但带错误", "artifact_warning": "产物缺失", "hotspot": "耗时热点",
                 "contract_violation": "产物契约违约", **FRAMEWORK_KINDS}
DIRECTIONS = {"reliability": "可靠性", "rework": "返工前置", "intervention": "人工干预沉淀",
              "latency": "关键路径耗时", "contract": "产物契约与口径", "interaction": "交互质量",
              "cost": "成本", "other": "其他"}
COUNT_KEYS = ("failures", "timeouts", "lifetime_kills", "rejects", "retasks", "bypasses",
              "provision_failures", "awaiting", "answers", "artifact_warnings", "contract_violations")
FLOW_STATUSES = ("selected", "skipped", "optimizing", "optimized", "gated", "gate_failed",
                 "reviewing", "changes_requested", "fixing", "approved", "merged", "abandoned")
_STATUS_BY_EVENT = {"flow_selected": "selected", "flow_skipped": "skipped",
                    "worktree_created": "optimizing", "optimizer_done": "optimized", "fix_done": "optimized",
                    "review_started": "reviewing", "fix_round": "fixing",
                    "merged": "merged", "abandoned": "abandoned"}
REVIEW_SEVERITIES = ("blocking", "major", "minor")
# 跨流程请求（optimize.json 的 cross_flow）：open/deferred 未闭环，done/rejected 已闭环
CROSS_FLOW_STATUSES = ("open", "done", "deferred", "rejected")
CROSS_FLOW_OPEN = ("open", "deferred")


# ---------------- 路径与小工具 ----------------

def campaigns_root(paths: Paths) -> Path:
    """campaign 明细（证据/日志/评审/报告）：运行时数据，随 shared/artifacts 不入库。"""
    return paths.artifacts_dir / "optimize"


def campaign_dir(paths: Paths, cid: str) -> Path:
    return campaigns_root(paths) / cid


def history_file(paths: Paths) -> Path:
    """优化历史（入库）：每次 campaign 的改动与指标基线，下次 collect 据此评估效果。"""
    return paths.root / "coordinator" / "insights" / "optimize-history.json"


def worktrees_root(paths: Paths, cid: str) -> Path:
    return paths.root / ".worktrees" / cid


def branch_name(cid: str, flow: str) -> str:
    return f"opt/{cid}/{flow}"


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def _median(xs: list[float]) -> float | None:
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 1)


def _max(xs: list[float]) -> float | None:
    s = [x for x in xs if x is not None]
    return round(max(s), 1) if s else None


def _fmt_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def resolve_window(since: str = "", until: str = "", days: int = 0,
                   today: date | None = None) -> dict[str, Any]:
    """时间窗（本地时区、按 Job 收敛时刻归属）。默认上一个自然周（周一~周日）。

    since/until 为 YYYY-MM-DD（含首尾）；days=N 表示截至今天的过去 N 天（含今天）。"""
    today = today or date.today()
    if since:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until) + timedelta(days=1) if until else today + timedelta(days=1)
        label = "指定区间"
    elif days:
        end = today + timedelta(days=1)
        start = end - timedelta(days=days)
        label = f"过去 {days} 天"
    else:
        monday = today - timedelta(days=today.weekday())
        start, end = monday - timedelta(days=7), monday
        label = "上一个自然周"
    if end <= start:
        raise ValueError(f"时间窗非法：{start} ~ {end}")
    to_ts = lambda d: time.mktime(datetime(d.year, d.month, d.day).timetuple())  # noqa: E731
    return {"label": label, "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
            "start_ts": to_ts(start), "end_ts": to_ts(end)}


def new_campaign_id(paths: Paths, today: date | None = None) -> str:
    base = f"opt-{(today or date.today()):%Y%m%d}"
    cid, n = base, 2
    while campaign_dir(paths, cid).exists():
        cid, n = f"{base}-{n}", n + 1
    return cid


# ---------------- 原始数据 ----------------

def load_events(paths: Paths) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    """run.log 按 goal_id 分组，保留行号（证据要能回指 run.log:<行>）。"""
    out: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    f = paths.state_dir / "run.log"
    if not f.exists():
        return out
    with open(f, encoding="utf-8") as fh:
        for no, line in enumerate(fh, 1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("goal_id"):
                out.setdefault(ev["goal_id"], []).append((no, ev))
    return out


def load_templates(paths: Paths) -> list[dict[str, Any]]:
    """plan 模板清单：名字、路径、非 fan-out 任务集合（用于把 Job 归属到模板）、各任务能力。"""
    out = []
    for f in sorted(paths.plans_dir.glob("*.json")):
        data = read_json_or(f, {})
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            continue
        tasks = [t for t in data["tasks"] if isinstance(t, dict) and t.get("task_id")]
        out.append({"name": f.stem, "path": str(f.relative_to(paths.root)), "goal_id": data.get("goal_id"),
                    "task_ids": {t["task_id"] for t in tasks},
                    "caps": [list(t.get("capabilities") or []) for t in tasks]})
    return out


def match_template(board: Board, templates: list[dict[str, Any]]) -> str:
    """与 retro._match_templates 同口径：模板 task_id 集合 == Job 的非 fan-out 子任务集合。"""
    base = {tid for tid, t in board.tasks.items() if not t.fanout_group}
    hits = [tp for tp in templates if tp["task_ids"] == base]
    hits.sort(key=lambda tp: (tp["goal_id"] != board.goal_id, tp["name"]))
    return hits[0]["name"] if hits else ADHOC


def _completed_ts(board: Board) -> float | None:
    if board.completed_at:
        return board.completed_at
    ends = [r.get("ended_at") for t in board.tasks.values() for r in t.runs if r.get("ended_at")]
    return max(ends) if ends and board.is_complete() else None


def window_boards(paths: Paths, window: dict[str, Any]) -> list[Board]:
    """窗口内已收敛、有真实执行的业务 Job（排除复盘 Job）。"""
    out = []
    for b in Board.load_all(paths):
        if retro.is_retro(b.goal_id) or not b.is_complete() or not retro._has_real_runs(b):
            continue
        ts = _completed_ts(b)
        if ts is not None and window["start_ts"] <= ts < window["end_ts"]:
            out.append(b)
    out.sort(key=lambda b: _completed_ts(b) or 0)
    return out


# ---------------- 单个 Job 的指标与信号 ----------------

def _task_key(t) -> str:
    """fan-out 子任务（build-variant.3）按模板 id 聚合，跨 Job 才可比。"""
    return t.fanout_group or t.task_id


def _sig(kind: str, gid: str, tid: str, detail: str, evidence: str, ts: float | None = None) -> dict[str, Any]:
    return {"kind": kind, "label": SIGNAL_LABELS.get(kind, kind), "goal_id": gid, "task_id": tid,
            "detail": detail[:300], "evidence": evidence, "ts": ts,
            "framework": kind in FRAMEWORK_KINDS}


def job_metrics(board: Board, events: list[tuple[int, dict[str, Any]]], now: float) -> dict[str, Any]:
    """一个 Job 的计时、计数与问题信号。证据统一写成 run.log:<行> 或 jobs/<gid>.json 字段路径。"""
    gid = board.goal_id
    counts = dict.fromkeys(COUNT_KEYS, 0)
    signals: list[dict[str, Any]] = []
    last_answer: dict[str, float] = {}
    violations: dict[str, int] = {}      # 契约违约按 _task_key 计，fan-out 子任务归到模板 id
    for no, ev in events:
        e, tid, ts = ev.get("event"), ev.get("task_id", ""), ev.get("ts")
        where = f"run.log:{no}"
        if e in ("task_failed", "task_fatal"):
            counts["failures"] += 1
            signals.append(_sig("failure", gid, tid, ev.get("reason") or ev.get("error") or e, where, ts))
        elif e == "task_timeout":
            reason = ev.get("reason", "")
            if "max lifetime" in reason:
                counts["lifetime_kills"] += 1
                signals.append(_sig("lifetime_kill", gid, tid, reason, where, ts))
            else:
                counts["timeouts"] += 1
                signals.append(_sig("timeout", gid, tid, reason, where, ts))
        elif e == "task_rejected":
            counts["rejects"] += 1
            signals.append(_sig("reject", gid, tid, f"被 {ev.get('from_task')} 打回：{ev.get('reason', '')}",
                                where, ts))
        elif e == "task_retasked":
            counts["retasks"] += 1
            signals.append(_sig("retask", gid, tid, f"{ev.get('mode')}：{ev.get('reason', '')}", where, ts))
        elif e == "task_bypassed":
            counts["bypasses"] += 1
            signals.append(_sig("bypass", gid, tid, ev.get("reason", ""), where, ts))
        elif e == "dispatch_provision_failed":
            counts["provision_failures"] += 1
            # create_pane 失败时没有 pane_id；stage/error 是较新的事件字段，有才附上
            pane = f"pane {ev['pane_id']} " if ev.get("pane_id") else ""
            why = "：".join(str(ev[k]) for k in ("stage", "error") if ev.get(k))
            signals.append(_sig("provision_failure", gid, tid,
                                f"{pane}第 {ev.get('provision_attempt')} 次拉起失败" + (f"（{why}）" if why else ""),
                                where, ts))
        elif e == "task_answered":
            counts["answers"] += 1
            last_answer[tid] = ts or 0
        elif e == "task_awaiting_input":
            counts["awaiting"] += 1
            q = str(ev.get("question") or "")
            if "Interrupted" in q or "What should Claude do instead" in q:
                signals.append(_sig("interrupt_misread", gid, tid, q, where, ts))
            elif tid in last_answer and ts is not None and ts - last_answer[tid] < 5:
                signals.append(_sig("awaiting_reentry", gid, tid,
                                    f"答复后 {ts - last_answer[tid]:.3f}s 再次进入等人：{q}", where, ts))
        elif e == "task_artifacts_missing":
            counts["artifact_warnings"] += 1
            signals.append(_sig("artifact_warning", gid, tid, f"缺失 {ev.get('missing')}", where, ts))
        elif e == "task_contract_violation":
            counts["contract_violations"] += 1
            t = board.tasks.get(tid)
            k = _task_key(t) if t else tid
            violations[k] = violations.get(k, 0) + 1
            items = ev.get("violations") or []
            detail = "；".join(map(str, items)) if isinstance(items, list) else str(items)
            signals.append(_sig("contract_violation", gid, tid, detail or e, where, ts))
    wall = board.wall_s(now)
    if wall is None:
        starts = [r["started_at"] for t in board.tasks.values() for r in t.runs if r.get("started_at")]
        end = _completed_ts(board)
        wall = round(end - min(starts), 3) if starts and end else None
    tasks: dict[str, dict[str, Any]] = {}
    awaiting_s = 0.0
    for t in board.tasks.values():
        if not t.runs:
            continue
        k = _task_key(t)
        row = tasks.setdefault(k, {"task_id": k, "agent": t.agent, "runs": 0, "active_s": 0.0,
                                   "span_s": 0.0, "shards": 0, "failed_runs": 0, "status": t.status,
                                   "contract_violations": violations.get(k, 0)})
        own = 0.0
        for r in t.runs:
            act = r.get("active_s") or 0.0
            own += act
            row["runs"] += 1
            row["active_s"] = round(row["active_s"] + act, 1)
            row["failed_runs"] += r.get("outcome") in FAIL_OUTCOMES
            awaiting_s += r.get("awaiting_s") or 0.0
        # span：本步在关键路径上的耗时。普通任务=各次运行（重试/返工）之和；fan-out=最慢那一片
        row["shards"] += 1
        row["span_s"] = round(max(row["span_s"], own), 1)
        if t.status == DONE and t.error:
            signals.append(_sig("soft_error", gid, t.task_id, t.error, f"jobs/{gid}.json tasks[{t.task_id}].error"))
        if t.artifact_warnings:
            signals.append(_sig("artifact_warning", gid, t.task_id, f"缺失 {t.artifact_warnings}",
                                f"jobs/{gid}.json tasks[{t.task_id}].artifact_warnings"))
    if wall and wall >= HOTSPOT_MIN_WALL_S:
        # 按 span 算占比：fan-out 取最慢一片，免得 6 路并行之和被误报成热点
        for row in tasks.values():
            span = row["span_s"]
            share = span / wall
            if share >= HOTSPOT_SHARE:
                signals.append(_sig("hotspot", gid, row["task_id"],
                                    f"净执行 {span:.0f}s，占墙钟 {wall:.0f}s 的 {share:.0%}",
                                    f"jobs/{gid}.json tasks[{row['task_id']}].runs"))
    return {"goal_id": gid, "goal": board.goal[:200], "created_at": board.created_at,
            "completed_at": _completed_ts(board), "wall_s": wall, "ok": not board.has_failures(),
            "awaiting_s": round(awaiting_s, 1), "counts": counts,
            "tasks": sorted(tasks.values(), key=lambda r: -r["active_s"]), "signals": signals,
            "agents": sorted({r.get("agent") for t in board.tasks.values() for r in t.runs if r.get("agent")})}


# ---------------- 流程聚合与指标词表 ----------------

def flow_metrics(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """一条流程在窗口内的聚合指标。计数类同时给总数与每 Job 均值（跨窗口对比用均值）。"""
    n = len(jobs)
    walls = [j["wall_s"] for j in jobs]
    totals = {k: sum(j["counts"][k] for j in jobs) for k in COUNT_KEYS}
    by_task: dict[str, dict[str, Any]] = {}
    for j in jobs:
        for row in j["tasks"]:
            a = by_task.setdefault(row["task_id"], {"task_id": row["task_id"], "agent": row["agent"],
                                                    "span": [], "runs": 0, "failed_runs": 0,
                                                    "contract_violations": 0})
            a["span"].append(row["span_s"])
            a["runs"] += row["runs"]
            a["failed_runs"] += row["failed_runs"]
            a["contract_violations"] += row.get("contract_violations", 0)
    tasks = []
    for a in by_task.values():
        spans = a.pop("span")
        tasks.append({**a, "jobs": len(spans), "span_p50_s": _median(spans), "span_max_s": _max(spans)})
    tasks.sort(key=lambda r: -(r["span_p50_s"] or 0))
    return {"jobs": n, "ok_jobs": sum(j["ok"] for j in jobs),
            "success_rate": round(sum(j["ok"] for j in jobs) / n, 3) if n else None,
            "wall_p50_s": _median(walls), "wall_max_s": _max(walls),
            "awaiting_s": round(sum(j["awaiting_s"] for j in jobs), 1),
            "totals": totals, "per_job": {k: round(v / n, 3) if n else None for k, v in totals.items()},
            "tasks": tasks}


COUNT_LABELS = {"failures": "失败", "timeouts": "超时", "lifetime_kills": "寿命上限回收", "rejects": "打回",
                "retasks": "人工改写 goal", "bypasses": "人工跳过", "provision_failures": "拉起失败",
                "awaiting": "进入等人", "answers": "人工答复",
                "artifact_warnings": "产物缺失告警", "contract_violations": "产物契约违约"}
METRIC_HELP = {
    "job.wall_s.p50": "Job 墙钟中位数(秒)", "job.wall_s.max": "Job 墙钟最大值(秒)",
    "job.success_rate": "无失败收敛的 Job 占比（越高越好）", "job.awaiting_s": "每 Job 等人累计(秒)",
    **{f"job.{k}": f"每 Job 平均{v}次数" for k, v in COUNT_LABELS.items()},
    "task:<task_id>.span_s.p50": "某步关键路径耗时中位数(秒)", "task:<task_id>.span_s.max": "某步耗时最大值(秒)",
    "task:<task_id>.failed_runs": "某步失败运行次数（每 Job 均值）", "task:<task_id>.runs": "某步运行次数（每 Job 均值）",
    "task:<task_id>.contract_violations": "某步产物契约违约次数（每 Job 均值）",
}
HIGHER_IS_BETTER = {"job.success_rate"}


def metric_value(m: dict[str, Any], key: str) -> float | None:
    """按指标词表取值；未知指标或无数据返回 None。

    旧 campaign / 历史里的指标可能早于新增的计数键（如 contract_violations），缺键同样返回 None（对比得 no_data）。"""
    if not m or not m.get("jobs"):
        return None
    if key == "job.wall_s.p50":
        return m["wall_p50_s"]
    if key == "job.wall_s.max":
        return m["wall_max_s"]
    if key == "job.success_rate":
        return m["success_rate"]
    if key == "job.awaiting_s":
        return round(m["awaiting_s"] / m["jobs"], 1)
    if key.startswith("job.") and key[4:] in COUNT_KEYS:
        return (m.get("per_job") or {}).get(key[4:])
    if key.startswith("task:"):
        tid, _, field_ = key[5:].partition(".")
        row = next((t for t in m["tasks"] if t["task_id"] == tid), None)
        if row is None:
            return None
        if field_ == "span_s.p50":
            return row["span_p50_s"]
        if field_ == "span_s.max":
            return row["span_max_s"]
        if field_ in ("failed_runs", "runs", "contract_violations"):
            return round(row[field_] / m["jobs"], 3) if row.get(field_) is not None else None
    return None


def valid_metric(key: str) -> bool:
    if key in METRIC_HELP:
        return True
    if key.startswith("task:") and "." in key[5:]:
        return "task:<task_id>." + key[5:].partition(".")[2] in METRIC_HELP
    return False


def compare(key: str, baseline: float | None, current: float | None) -> str:
    """improved / unchanged / regressed / no_data（相对变化 5% 以内算持平）。"""
    if baseline is None or current is None:
        return "no_data"
    if baseline == current or (baseline and abs(current - baseline) / abs(baseline) < 0.05):
        return "unchanged"
    better = current > baseline if key in HIGHER_IS_BETTER else current < baseline
    return "improved" if better else "regressed"


# ---------------- 改动边界：共享 agent 定 owner ----------------

# 任何流程都不许改：框架代码、测试、协调者协议、全局配置、运行时状态与产物、agent manifest
FORBIDDEN_PREFIXES = ("agent_team/", "tests/", "coordinator/prompt.md", "coordinator/routing.md",
                      "coordinator/state/", "coordinator/insights/", "team.yaml", ".claude/", "shared/",
                      "CLAUDE.md", "README", "pyproject.toml", "uv.lock", ".gitignore", ".worktrees/")


def forbidden(rel: str) -> bool:
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "agent.yaml":
        return True
    return rel.startswith(FORBIDDEN_PREFIXES)


def template_agents(paths: Paths, templates: list[dict[str, Any]]) -> dict[str, set[str]]:
    """agent → 可能用到它的 plan 模板（按能力静态匹配），用来提示共享 agent 的跨流程回归风险。"""
    agents = registry.list_agents(paths)
    out: dict[str, set[str]] = {}
    for tp in templates:
        for caps in tp["caps"]:
            for m in agents:
                if caps and set(caps) & set(m.capabilities):
                    out.setdefault(m.name, set()).add(tp["name"])
    return out


def assign_scopes(flows: dict[str, dict[str, Any]], selected: list[str],
                  tpl_agents: dict[str, set[str]]) -> None:
    """为入选流程划改动边界：共享 agent 归窗口内 Job 数最多的入选流程（同数按名字），其余只能提跨流程请求。"""
    users: dict[str, list[str]] = {}
    for name in selected:
        for a in flows[name]["agents"]:
            users.setdefault(a, []).append(name)
    owner = {a: sorted(fs, key=lambda f: (-flows[f]["metrics"]["jobs"], f))[0] for a, fs in users.items()}
    for name in selected:
        f = flows[name]
        f["owned_agents"] = sorted(a for a in f["agents"] if owner.get(a) == name)
        f["requests_agents"] = {a: owner[a] for a in f["agents"] if owner.get(a) not in (None, name)}
        f["shared_agents"] = {a: sorted(tpl_agents.get(a, set()) - {name}) for a in f["owned_agents"]
                              if tpl_agents.get(a, set()) - {name}}
        f["allowed"] = [f["plan"]] + [p for a in f["owned_agents"]
                                      for p in (f"agents/{a}/prompt.md", f"agents/{a}/workspace/")]


def allowed(flow: dict[str, Any], rel: str) -> bool:
    if forbidden(rel):
        return False
    return any(rel == p or (p.endswith("/") and rel.startswith(p))
               for p in flow.get("allowed", []) + flow.get("extra_allowed", []))


# ---------------- 优化历史与上次改动的效果 ----------------

def load_history(paths: Paths) -> dict[str, Any]:
    h = read_json_or(history_file(paths), {})
    if not isinstance(h, dict):
        h = {}
    h.setdefault("version", 1)
    h.setdefault("campaigns", [])
    return h


def merged_bookmarks(paths: Paths, exclude: str = "") -> dict[str, dict[str, Any]]:
    """书签：每条流程最近一次合并进主分支的优化（取自入库的优化历史）。

    finished_at 之前开始的 Job 跑的是改动前的流程，collect 不拿它们计分，previous_effect 也不拿它们算效果。"""
    out: dict[str, dict[str, Any]] = {}
    for c in load_history(paths)["campaigns"]:
        if c.get("campaign_id") == exclude or not c.get("finished_at"):
            continue
        for name, f in (c.get("flows") or {}).items():
            if f.get("status") == "merged" and c["finished_at"] > out.get(name, {}).get("finished_at", 0):
                out[name] = {"campaign_id": c["campaign_id"], "window": c.get("window") or {},
                             "finished_at": c["finished_at"], "changes": f.get("changes") or []}
    return out


def previous_effect(paths: Paths, cid: str, job_rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """每条流程最近一次合并的改动在本窗口的效果。只统计该次优化完成之后才开始的 Job，
    窗口里全是改动前的运行就记 no_data，不拿旧数据冒充效果。"""
    out = []
    for name, last in sorted(merged_bookmarks(paths, exclude=cid).items()):
        after = [j for j in job_rows.get(name, []) if (j["created_at"] or 0) >= last["finished_at"]]
        m = flow_metrics(after) if after else {}
        for ch in last["changes"]:
            key = ch.get("metric") or ""
            cur = metric_value(m, key) if key else None
            out.append({"campaign_id": last["campaign_id"], "flow": name, "title": ch.get("title", ""),
                        "direction": ch.get("direction", ""), "metric": key, "baseline": ch.get("baseline"),
                        "current": cur, "jobs_after": len(after),
                        "verdict": compare(key, ch.get("baseline"), cur) if key else "no_data"})
    return out


def _metric_task(metric: str) -> str:
    """task:<task_id>.<指标后缀> 里的 task_id；job.* 等非单步指标返回空串。"""
    if not metric.startswith("task:"):
        return ""
    tid, dot, _ = metric[len("task:"):].partition(".")
    return tid if dot else ""


def previous_changes(effects: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """按 (flow, task_id) 索引上次优化里针对该步的改动，供问题信号标注「相关上次改动」。

    只做提示，不做自动抑制：改动后可能还没有新 Job，效果未验证。同一 (flow, task) 只关联一条改动。"""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for e in effects:
        tid = _metric_task(e.get("metric") or "")
        if tid:
            out.setdefault((e["flow"], tid), {"title": e["title"], "metric": e["metric"],
                                              "current": e["current"], "jobs_after": e["jobs_after"],
                                              "verdict": e["verdict"]})
    return out


# ---------------- 书签：上次优化到哪 ----------------

def covered_until(paths: Paths) -> date | None:
    """已收尾 campaign 覆盖到的最晚日期（含）。放弃或未收尾的 campaign 不算覆盖。"""
    ends = [(c.get("window") or {}).get("end") for c in load_history(paths)["campaigns"] if c.get("finished_at")]
    return max((date.fromisoformat(e) for e in ends if e), default=None)


def default_window(paths: Paths, today: date | None = None) -> dict[str, Any]:
    """不指定时间窗时从书签续接：上次覆盖日的次日 ~ 昨天；没有优化历史时退回上一个自然周。"""
    today = today or date.today()
    last = covered_until(paths)
    if last is None:
        return resolve_window(today=today)
    start, end = last + timedelta(days=1), today - timedelta(days=1)
    if start > end:
        raise ValueError(f"书签已覆盖到 {last}，还没有新的完整日期；要重看请用 --since/--until 或 --days")
    w = resolve_window(since=start.isoformat(), until=end.isoformat(), today=today)
    w["label"] = f"书签续接（上次覆盖到 {last}）"
    return w


def unfinished_campaigns(paths: Paths) -> list[str]:
    """既没 finish 也没 abandon 的 campaign。"""
    root = campaigns_root(paths)
    out = []
    for p in sorted(root.glob("*/campaign.json")) if root.exists() else []:
        c = read_json_or(p, None)
        if isinstance(c, dict) and not c.get("finished_at") and not c.get("abandoned_at"):
            out.append(p.parent.name)
    return out


def abandon_campaign(paths: Paths, cid: str, reason: str = "") -> dict[str, Any]:
    """把半途废弃的 campaign 标成放弃，不再挡 collect。已有流程合并的应 finish 收尾，不许放弃。"""
    c = load_campaign(paths, cid)
    if c.get("finished_at"):
        raise ValueError(f"campaign {cid} 已 finish，不能放弃")
    merged = [n for n, f in c["flows"].items() if f["status"] == "merged"]
    if merged:
        raise ValueError(f"campaign {cid} 已有流程合并进主分支（{', '.join(merged)}），应 `team optimize finish` 收尾")
    return log(paths, cid, "campaign_abandoned", reason=reason)


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def bookmark(paths: Paths, today: date | None = None) -> dict[str, Any]:
    """上次优化到哪：已覆盖的时间窗、下次默认窗口、每个 plan 模板最近一次优化及之后的新 Job 数、未收尾 campaign。"""
    h = load_history(paths)
    done = sorted((c for c in h["campaigns"] if c.get("finished_at")), key=lambda c: c["finished_at"])
    marks = merged_bookmarks(paths)
    templates = load_templates(paths)
    started: dict[str, list[float]] = {}
    for b in Board.load_all(paths):
        if not retro.is_retro(b.goal_id) and b.is_complete() and retro._has_real_runs(b):
            started.setdefault(match_template(b, templates), []).append(b.created_at or 0)
    flows: dict[str, dict[str, Any]] = {tp["name"]: {"flow": tp["name"], "last_attempt": None, "last_merged": None}
                                        for tp in templates}
    for c in done:
        w = c.get("window") or {}
        for name, f in (c.get("flows") or {}).items():
            row = flows.setdefault(name, {"flow": name, "last_attempt": None, "last_merged": None})
            row["last_attempt"] = {"campaign_id": c["campaign_id"], "status": f.get("status"),
                                   "window": f"{w.get('start')}~{w.get('end')}"}
    for name, row in flows.items():
        m = marks.get(name)
        if m:
            row["last_merged"] = {"campaign_id": m["campaign_id"], "finished_at": _fmt_time(m["finished_at"]),
                                  "changes": [ch.get("title", "") for ch in m["changes"]]}
        # 上次合并之后（从未优化过=全部）收敛的业务 Job 数：为 0 说明改动效果还没被验证
        row["new_jobs"] = sum(t >= (m["finished_at"] if m else 0) for t in started.get(name, []))
    try:
        nxt = {k: v for k, v in default_window(paths, today).items() if k in ("label", "start", "end")}
    except ValueError as e:
        nxt = {"error": str(e)}
    last = covered_until(paths)
    return {"covered_until": last.isoformat() if last else None, "next_window": nxt,
            "campaigns": [{"campaign_id": c["campaign_id"], "window": c.get("window"),
                           "finished_at": _fmt_time(c["finished_at"])} for c in done],
            "unfinished": unfinished_campaigns(paths),
            "flows": [flows[n] for n in sorted(flows) if n != ADHOC]}


# ---------------- 跨流程请求：结构化与跨 campaign 闭环 ----------------

def _cross_flow_id(requester: str, owner: str, request: str) -> str:
    """稳定 id：发起流程 + owner + 请求原文（空白归一）的指纹，下一轮 owner 引用它来闭环。"""
    key = "|".join((requester, owner, " ".join(request.split())))
    return "xf-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _infer_owner(text: str, requests_agents: dict[str, str]) -> str:
    """旧字符串请求里点名了哪个不归本流程的 agent，就取它的 owner 流程；点名多个 owner 时不猜。
    边界只认 ASCII 字母数字/下划线/连字符，agent 名紧贴汉字（「请html-gen收敛」）也能认出。"""
    owners = {o for a, o in requests_agents.items()
              if re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", text, re.ASCII)}
    return owners.pop() if len(owners) == 1 else ""


def normalize_cross_flow(items: Any, requester: str,
                         requests_agents: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """把 optimize.json 的 cross_flow 归一成对象数组。

    对象项保留 owner/target_flow/request/evidence/status/verification；字符串项（旧交付）当作 open 请求，
    owner 按 requests_agents 推断；没有 request 的对象和其他类型丢弃。status 不在取值内按 open。
    对象自带 id（引用上一轮 pending_cross_flow 的 id 来闭环）就沿用，否则按指纹生成；自带 requester
    （owner 替别的流程闭环时照抄）也沿用。"""
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            it = {"request": it, "owner": _infer_owner(it, requests_agents or {})}
        if not isinstance(it, dict):
            continue
        request = str(it.get("request") or "").strip()
        if not request:
            continue
        who = str(it.get("requester") or "").strip() or requester
        owner, target = str(it.get("owner") or "").strip(), str(it.get("target_flow") or "").strip()
        status = str(it.get("status") or "").strip().lower()
        ev = it.get("evidence")
        out.append({"id": str(it.get("id") or "").strip() or _cross_flow_id(who, owner, request),
                    "requester": who, "owner": owner, "target_flow": target, "request": request,
                    "evidence": [str(x) for x in ev] if isinstance(ev, list) else ([str(ev)] if ev else []),
                    "status": status if status in CROSS_FLOW_STATUSES else "open",
                    "verification": str(it.get("verification") or "").strip()})
    return out


def cross_flow_ledger(campaigns: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """跨流程请求的闭环判定（collect 的 pending_cross_flow 与报告的未闭环计数共用这一个口径）。

    campaigns 按收尾先后排列，每个含 campaign_id、flows{名: {status, cross_flow(已归一化)}}。
    只看已合并的流程：没合并的改动不生效，它提的请求与闭环都不算。每个流程取最近一次合并时的条目；
    open/deferred 项若在同一次或之后被任一已合并流程把同 id 标成 done/rejected 即闭环；同 id 按时间覆盖。
    发起流程后来再次合并却没再提同一请求，视为不再需要。返回 (未闭环 {id: 条目}, 已闭环 {id: 闭环的流程})。"""
    latest: dict[str, tuple[int, str, list[dict[str, Any]]]] = {}
    closed: dict[str, tuple[int, str]] = {}
    for i, c in enumerate(campaigns):
        for name, f in sorted((c.get("flows") or {}).items()):
            if f.get("status") != "merged":
                continue
            items = [x for x in f.get("cross_flow") or [] if isinstance(x, dict) and x.get("id")]
            latest[name] = (i, c.get("campaign_id", ""), items)
            for x in items:
                if x.get("status") in ("done", "rejected"):
                    closed[x["id"]] = (i, name)
    open_: dict[str, dict[str, Any]] = {}
    for name, (i, cid, items) in sorted(latest.items(), key=lambda kv: (kv[1][0], kv[0])):
        for x in items:
            if x.get("status") in CROSS_FLOW_OPEN and closed.get(x["id"], (-1, ""))[0] < i:
                open_[x["id"]] = {**x, "requester": x.get("requester") or name, "campaign_id": cid}
    return open_, {k: v[1] for k, v in closed.items() if k not in open_}


def pending_cross_flow(paths: Paths, exclude: str = "") -> list[dict[str, Any]]:
    """上次未闭环的跨流程请求：按 cross_flow_ledger 口径扫全部已收尾 campaign 的优化历史。"""
    done = sorted((c for c in load_history(paths)["campaigns"]
                   if c.get("finished_at") and c.get("campaign_id") != exclude), key=lambda c: c["finished_at"])
    return list(cross_flow_ledger(done)[0].values())


# ---------------- campaign 状态与优化日志 ----------------

def _campaign_file(paths: Paths, cid: str) -> Path:
    return campaign_dir(paths, cid) / "campaign.json"


def load_campaign(paths: Paths, cid: str) -> dict[str, Any]:
    c = read_json_or(_campaign_file(paths, cid), None)
    if not isinstance(c, dict):
        raise FileNotFoundError(f"campaign {cid} 不存在或已损坏：{_campaign_file(paths, cid)}")
    return c


def save_campaign(paths: Paths, c: dict[str, Any]) -> None:
    c["updated_at"] = round(time.time(), 3)
    atomic_write_json(_campaign_file(paths, c["campaign_id"]), c)


def latest_campaign(paths: Paths) -> str:
    """最近一次 campaign；放弃的不算（放弃会刷新 mtime，不能让它顶替正在跑的那个）。"""
    root = campaigns_root(paths)
    cands = sorted((p for p in root.glob("*/campaign.json")), key=lambda p: p.stat().st_mtime) if root.exists() else []
    live = [p for p in cands if not (read_json_or(p, {}) or {}).get("abandoned_at")]
    if not (live or cands):
        raise FileNotFoundError("还没有 campaign，先跑 `team optimize collect`")
    return (live or cands)[-1].parent.name


def log(paths: Paths, cid: str, event: str, flow: str = "", **fields: Any) -> dict[str, Any]:
    """追加一条优化日志；状态类事件同步推进 campaign.json 里该流程的状态。"""
    ev = {"ts": round(time.time(), 3), "event": event, **({"flow": flow} if flow else {}), **fields}
    d = campaign_dir(paths, cid)
    with file_lock(_campaign_file(paths, cid)):
        c = load_campaign(paths, cid)
        f = c["flows"].get(flow) if flow else None
        if flow and f is None:
            raise KeyError(f"campaign {cid} 里没有流程 {flow}")
        if f is not None:
            if event == "fix_round" and f.get("fix_rounds", 0) >= MAX_FIX_ROUNDS:
                raise ValueError(f"{flow} 已修复 {MAX_FIX_ROUNDS} 轮仍未通过评审，按准则应 abandoned，不再修")
            if event in _STATUS_BY_EVENT:
                f["status"] = _STATUS_BY_EVENT[event]
            if event == "worktree_created":
                f["worktree"], f["branch"] = fields.get("worktree"), fields.get("branch")
            elif event in ("optimizer_done", "fix_done") and fields.get("commit"):
                f.setdefault("commits", []).append(fields["commit"])
            elif event == "gate_result":
                f["gate"] = {k: fields.get(k) for k in ("ok", "round", "file")}
                f["status"] = "gated" if fields.get("ok") else "gate_failed"
            elif event == "review_verdict":
                f["review_rounds"] = int(fields.get("round") or f.get("review_rounds", 0) + 1)
                f["verdict"] = fields.get("verdict")
                f["status"] = "approved" if fields.get("verdict") == "PASS" else "changes_requested"
            elif event == "fix_round":
                f["fix_rounds"] = f.get("fix_rounds", 0) + 1
            elif event == "merged":
                f["merge_commit"], f["head"] = fields.get("commit"), fields.get("head")
                if fields.get("commits"):
                    f["commits"] = list(fields["commits"])
            elif event == "scope_extended":
                extra = [p for p in fields.get("files", []) if not forbidden(p)]
                f["extra_allowed"] = sorted(set(f.get("extra_allowed", [])) | set(extra))
            elif event == "abandoned":
                f["reason"] = fields.get("reason", f.get("reason", ""))
        elif event == "main_gate":
            c["main_gate"] = {k: fields.get(k) for k in ("ok", "file", "commit")}
        elif event == "framework_issue_recorded":
            c.setdefault("framework_issue_ids", [])
            c["framework_issue_ids"] += [i for i in fields.get("ids", []) if i not in c["framework_issue_ids"]]
        elif event == "campaign_abandoned":
            c["abandoned_at"], c["abandon_reason"] = ev["ts"], fields.get("reason", "")
        with open(d / "optimize-log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        save_campaign(paths, c)
    return ev


def read_log(paths: Paths, cid: str) -> list[dict[str, Any]]:
    f = campaign_dir(paths, cid) / "optimize-log.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------- collect ----------------

def _framework_candidates(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for s in signals:
        if not s["framework"]:
            continue
        g = by.setdefault(s["kind"], {"kind": s["kind"], "label": s["label"], "count": 0,
                                      "flows": set(), "jobs": set(), "evidence": []})
        g["count"] += 1
        g["flows"].add(s["flow"])
        g["jobs"].add(s["goal_id"])
        if len(g["evidence"]) < 10:
            g["evidence"].append(f"{s['evidence']} {s['goal_id']}/{s['task_id']}：{s['detail']}")
    return [{**g, "flows": sorted(g["flows"]), "jobs": sorted(g["jobs"])}
            for g in sorted(by.values(), key=lambda g: -g["count"])]


def collect(paths: Paths, window: dict[str, Any], cid: str = "", max_flows: int = MAX_FLOWS,
            now: float | None = None, force: bool = False) -> dict[str, Any]:
    """收集证据、选流程、划边界，落 campaign.json 与 evidence/。返回 campaign。

    有未收尾的 campaign 时拒绝新建（force 可跳过）；早于该流程上次优化合并的 Job 标 stale，不计分。"""
    now = now or time.time()
    cid = cid or new_campaign_id(paths)
    d = campaign_dir(paths, cid)
    if (d / "campaign.json").exists():
        raise FileExistsError(f"campaign {cid} 已存在：{d}")
    pending = unfinished_campaigns(paths)
    if pending and not force:
        raise RuntimeError(f"还有未收尾的 campaign：{', '.join(pending)}。先续跑并 `team optimize finish`，"
                           "或 `team optimize abandon --campaign <id> --reason …` 放弃；确需并行新建加 --force")
    (d / "evidence").mkdir(parents=True, exist_ok=True)
    templates = load_templates(paths)
    events = load_events(paths)
    marks = merged_bookmarks(paths, exclude=cid)
    job_rows: dict[str, list[dict[str, Any]]] = {}
    for b in window_boards(paths, window):
        row = job_metrics(b, events.get(b.goal_id, []), now)
        flow = match_template(b, templates)
        since = marks.get(flow, {}).get("finished_at")
        row["stale"] = bool(since) and (row["created_at"] or 0) < since
        for s in row["signals"]:
            s["flow"], s["stale"] = flow, row["stale"]
        job_rows.setdefault(flow, []).append(row)
    flows: dict[str, dict[str, Any]] = {}
    plan_of = {tp["name"]: tp["path"] for tp in templates}
    effects = previous_effect(paths, cid, job_rows)
    prev_changes = previous_changes(effects)
    for rows in job_rows.values():
        for r in rows:
            for s in r["signals"]:
                ch = prev_changes.get((s["flow"], s["task_id"]))
                s["previous_changes"] = [ch] if ch else []
    for name, rows in sorted(job_rows.items()):
        sigs = [s for r in rows for s in r["signals"]]
        fresh = [r for r in rows if not r["stale"]]
        score = sum(SIGNAL_WEIGHTS.get(s["kind"], 0) for s in sigs if not s["framework"] and not s["stale"])
        last = marks.get(name)
        flows[name] = {"name": name, "plan": plan_of.get(name, ""), "status": "skipped", "score": score,
                       "reason": "", "jobs": [r["goal_id"] for r in rows],
                       "stale_jobs": [r["goal_id"] for r in rows if r["stale"]],
                       "last_merged": {"campaign_id": last["campaign_id"], "finished_at": last["finished_at"]}
                       if last else None,
                       "agents": sorted({a for r in rows for a in r["agents"]}),
                       # 指标是改动的基线：有改动后的 Job 就只用它们，全是改动前的才退回全部（仅供报告）
                       "metrics": flow_metrics(fresh or rows),
                       "signal_counts": {k: sum(s["kind"] == k for s in sigs) for k in sorted({s["kind"] for s in sigs})},
                       "owned_agents": [], "requests_agents": {}, "shared_agents": {}, "allowed": [],
                       "extra_allowed": [], "worktree": None, "branch": None, "commits": [],
                       "review_rounds": 0, "fix_rounds": 0, "gate": None, "verdict": None}
    ranked_flows = sorted((f for f in flows.values() if f["name"] != ADHOC and f["score"] > 0),
                          key=lambda f: (-f["score"], -f["metrics"]["jobs"], f["name"]))
    selected = [f["name"] for f in ranked_flows[:max_flows]]
    for f in flows.values():
        stale = len(f["stale_jobs"])
        note = f"；另有 {stale} 个 Job 早于上次优化 {f['last_merged']['campaign_id']}，不计分" if stale else ""
        if f["name"] == ADHOC:
            f["reason"] = "未匹配到 plan 模板的临时 Job，只记录不优化"
        elif f["name"] in selected:
            f["status"], f["reason"] = "selected", f"问题信号得分 {f['score']}{note}"
        elif f["score"] > 0:
            f["reason"] = f"超出单次上限 {max_flows} 条，留待下次{note}"
        elif stale and stale == len(f["jobs"]):
            f["reason"] = (f"窗口内 {stale} 个 Job 都早于上次优化 {f['last_merged']['campaign_id']} 的合并，"
                           "不计分，等改动后的新 Job 验证效果")
        else:
            f["reason"] = f"健康：窗口内无问题信号{note}"
    assign_scopes(flows, selected, template_agents(paths, templates))
    all_sigs = [s for rows in job_rows.values() for r in rows for s in r["signals"]]
    try:
        base = _git(paths.root, "rev-parse", "--short", "HEAD")
        branch = _git(paths.root, "rev-parse", "--abbrev-ref", "HEAD")
    except (RuntimeError, OSError):
        base, branch = "", ""
    c = {"campaign_id": cid, "created_at": round(now, 3), "window": window, "max_flows": max_flows,
         "base_commit": base, "base_branch": branch, "flows": flows, "selected": selected,
         "framework_candidates": _framework_candidates(all_sigs),
         "previous_effect": effects, "pending_cross_flow": pending_cross_flow(paths, exclude=cid),
         "framework_issue_ids": [], "main_gate": None}
    atomic_write_json(d / "evidence" / "flows.json",
                      {"campaign_id": cid, "window": window, "jobs": job_rows})
    for name in selected:
        (d / "evidence" / f"{name}.md").write_text(evidence_markdown(c, name, job_rows[name]), encoding="utf-8")
    save_campaign(paths, c)
    log(paths, cid, "campaign_started", window=f"{window['start']}~{window['end']}", base_commit=base)
    log(paths, cid, "evidence_collected", jobs=sum(len(r) for r in job_rows.values()), flows=len(flows),
        framework_candidates=len(c["framework_candidates"]))
    for f in flows.values():
        log(paths, cid, "flow_selected" if f["status"] == "selected" else "flow_skipped", flow=f["name"],
            score=f["score"], reason=f["reason"])
    return load_campaign(paths, cid)


def _s(v: Any) -> str:
    return "—" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))


def evidence_markdown(c: dict[str, Any], name: str, rows: list[dict[str, Any]]) -> str:
    """交给优化者的证据摘要：指标、各步耗时、问题信号（带 run.log 行号）、改动边界、上次效果。"""
    f = c["flows"][name]
    m = f["metrics"]
    w = c["window"]
    totals = "、".join(f"{k}={v}" for k, v in m["totals"].items() if v)
    lines = [f"# {name} 证据摘要（{c['campaign_id']}）", "",
             f"- 时间窗：{w['start']} ~ {w['end']}（{w['label']}）；plan：`{f['plan']}`",
             f"- Job：{m['jobs']} 个，无失败收敛 {m['ok_jobs']} 个；墙钟 p50 {_s(m['wall_p50_s'])}s、"
             f"最大 {_s(m['wall_max_s'])}s；等人累计 {_s(m['awaiting_s'])}s",
             f"- 计数（总数）：{totals or '全 0'}",
             *([f"- 标「改动前」的 {len(f['stale_jobs'])} 个 Job 早于上次优化 {f['last_merged']['campaign_id']} 的合并，"
                "跑的是旧流程：其信号不计分、指标不进基线，只作背景；别针对已改过的问题重复改"]
               if f.get("stale_jobs") else []),
             "", "## 各 Job", "", "| goal_id | 墙钟(s) | 无失败 | 计数 |", "|---|---|---|---|"]
    for r in rows:
        cnt = "、".join(f"{k}={v}" for k, v in r["counts"].items() if v) or "—"
        gid = f"{r['goal_id']}（改动前）" if r.get("stale") else r["goal_id"]
        lines.append(f"| {gid} | {_s(r['wall_s'])} | {'是' if r['ok'] else '否'} | {cnt} |")
    lines += ["", "## 各步耗时（span=关键路径耗时：普通任务含重试之和，fan-out 取最慢一片）", "",
              "| task | agent | Job 数 | span p50(s) | span 最大(s) | 运行次数 | 失败运行 |", "|---|---|---|---|---|---|---|"]
    for t in m["tasks"]:
        lines.append(f"| {t['task_id']} | {t['agent']} | {t['jobs']} | {_s(t['span_p50_s'])} | "
                     f"{_s(t['span_max_s'])} | {t['runs']} | {t['failed_runs']} |")
    lines += ["", "## 问题信号", ""]
    sigs = [s for r in rows for s in r["signals"]]
    if not sigs:
        lines.append("- 无")
    for s in sigs:
        mark = "（改动前，不计分）" if s.get("stale") else ""
        lines.append(f"- [{s['label']}]{mark} {s['goal_id']}/{s['task_id']}：{s['detail']}（{s['evidence']}）")
        for ch in s.get("previous_changes") or []:
            if ch["jobs_after"] == 0:
                lines.append(f"  - 相关上次改动：{ch['title']}（{ch['metric']}）。改动后尚无新 Job，"
                             "效果未验证，不能据此判定已修复；优先核实该问题是否仍存在。")
            else:
                lines.append(f"  - 相关上次改动：{ch['title']}（{ch['metric']}）：当前 {_s(ch['current'])}，"
                             f"{ch['verdict']}（改动后该流程共 {ch['jobs_after']} 个 Job）。")
    lines += ["", "## 改动边界", "", "可改（白名单，`team optimize check` 会校验）："]
    lines += [f"- `{p}`" for p in f["allowed"]]
    for a, others in f["shared_agents"].items():
        lines.append(f"- 注意：`{a}` 也被 {', '.join(others)} 使用，新增行为必须加条件，不能影响这些流程")
    for a, owner in f["requests_agents"].items():
        lines.append(f"- `{a}` 归 {owner} 所有，本流程不能改，只能在 optimize.json 的 cross_flow 里提请求")
    eff = [e for e in c["previous_effect"] if e["flow"] == name]
    if eff:
        lines += ["", "## 上次优化的效果", ""]
        lines += [f"- {e['title']}（{e['metric']}）：基线 {_s(e['baseline'])} → 现在 {_s(e['current'])}，"
                  f"{e['verdict']}（改动后 {e['jobs_after']} 个 Job）" for e in eff]
    xf = [x for x in c.get("pending_cross_flow") or [] if name in (x.get("owner"), x.get("target_flow"),
                                                                   x.get("requester"))]
    if xf:
        lines += ["", "## 上次未闭环的跨流程请求", "",
                  "owner/目标流程是本流程的：处理后在本次 optimize.json 的 cross_flow 写同 `id`、"
                  "`status: \"done\"` 并填 `verification`（怎么验证已处理）；暂不处理写 `deferred`、不做写 `rejected`，"
                  "都在 verification 说明原因。发起流程是本流程的：仍需要就在本次 cross_flow 沿用同 `id` 再提，"
                  "不再提视为不再需要。", ""]
        for x in xf:
            to = x.get("owner") or "未指定"
            tgt = f"，目标流程 {x['target_flow']}" if x.get("target_flow") else ""
            lines.append(f"- `{x['id']}` {x['requester']} → owner {to}{tgt}（{x['campaign_id']}，{x['status']}）："
                         f"{x['request']}")
    return "\n".join(lines) + "\n"


# ---------------- 框架问题 → 建议池 ----------------

def record_framework_issues(paths: Paths, cid: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    """按 backlog schema 并入建议池（scope=project、category=framework），复用 retro 的指纹去重与复现计数。"""
    bf = retro.backlog_file(paths)
    stats = {"new": 0, "recurred": 0, "reopened": 0, "merged": 0, "ids": []}
    with file_lock(bf):
        data = retro._load_backlog(paths)
        now = round(time.time(), 3)
        for s in issues:
            if not isinstance(s, dict) or not str(s.get("title", "")).strip():
                continue
            s = {**s, "scope": "project", "category": s.get("category") or "framework"}
            by_id = {it["id"]: it for it in data["items"]}
            iid = s.get("dup_of") if s.get("dup_of") in by_id else retro._fingerprint(s)
            same = iid in by_id and cid in by_id[iid]["sources"]
            kind = retro._merge(data["items"], s, cid, now)
            # 同一 campaign 里多个流程/评审报了同一问题：只合并证据，不算复现
            stats["merged" if same else kind] += 1
            for it in data["items"]:
                if it["id"] == iid:
                    it["updated_at"] = now
            if iid not in stats["ids"]:
                stats["ids"].append(iid)
        atomic_write_json(bf, data)
    log(paths, cid, "framework_issue_recorded", ids=stats["ids"], new=stats["new"], recurred=stats["recurred"])
    return stats


def gather_framework_issues(paths: Paths, cid: str, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """汇总各流程 optimize.json 与评审 review-r*.json 里的 framework_issues，外加编排者补充的条目。"""
    out: list[dict[str, Any]] = []
    root = campaign_dir(paths, cid) / "flows"
    files = sorted(root.glob("*/optimize.json")) + sorted(root.glob("*/review-r*.json")) if root.exists() else []
    for p in files:
        data = read_json_or(p, {})
        for it in (data.get("framework_issues") or []) if isinstance(data, dict) else []:
            if isinstance(it, dict) and str(it.get("title", "")).strip():
                ev = list(it.get("evidence") or [])
                out.append({**it, "evidence": ev + [f"来源：{p.relative_to(campaign_dir(paths, cid))}"]})
    out += [it for it in (extra or []) if isinstance(it, dict)]
    return out


# ---------------- 记录优化者与评审员的交付 ----------------

def record_optimizer(paths: Paths, cid: str, flow: str) -> dict[str, Any]:
    """优化者交付后调用：读 worktree HEAD 与 optimize.json，记 optimizer_done（修复轮记 fix_done）。"""
    c = load_campaign(paths, cid)
    f = c["flows"][flow]
    head = _git(Path(f["worktree"]), "rev-parse", "--short", "HEAD")
    data = read_json_or(flow_dir(paths, cid, flow) / "optimize.json", {}) or {}
    event = "fix_done" if f.get("fix_rounds") else "optimizer_done"
    # 回显每条跨流程请求的 id：编排者转给 owner 时带上，owner 写同 id + done 即可在同一 campaign 闭环
    xf = [{k: x[k] for k in ("id", "owner", "status")} | {"request": x["request"][:80]}
          for x in normalize_cross_flow(data.get("cross_flow"), flow, f.get("requests_agents") or {})]
    return log(paths, cid, event, flow=flow, commit=head, changes=len(data.get("changes") or []),
               **({"cross_flow": xf} if xf else {}))


def record_review(paths: Paths, cid: str, flow: str) -> dict[str, Any]:
    """评审员交付后调用：取最新一轮 review-r<N>.json，校验后记 review_verdict。"""
    reviews = _rounds(flow_dir(paths, cid, flow), "review")
    if not reviews:
        raise FileNotFoundError(f"{flow} 还没有 review-r<N>.json")
    n, rv = reviews[-1]
    verdict = str(rv.get("verdict", "")).upper()
    if verdict not in ("PASS", "FAIL"):
        raise ValueError(f"review-r{n}.json 的 verdict 必须是 PASS/FAIL，得到 {rv.get('verdict')!r}")
    findings = [x for x in rv.get("findings") or [] if isinstance(x, dict)]
    blocking = [x for x in findings if x.get("severity") == "blocking" and x.get("status") != "fixed"]
    if verdict == "PASS" and blocking:
        raise ValueError(f"review-r{n}.json 判 PASS 却仍有 {len(blocking)} 条未修复的 blocking 问题")
    return log(paths, cid, "review_verdict", flow=flow, round=n, verdict=verdict,
               blocking=len(blocking), findings=len(findings))


# ---------------- subagent 提示词 ----------------

def templates_dir(paths: Paths) -> Path:
    return paths.root / ".claude" / "skills" / "team-optimize" / "templates"


def render_prompt(paths: Paths, cid: str, flow: str, role: str, round_: int = 0) -> str:
    """把 skill 里的优化者/评审员模板填成可直接派发的提示词，避免编排者手填漏项。"""
    if role not in ("optimizer", "reviewer"):
        raise ValueError(f"role 只能是 optimizer/reviewer，得到 {role!r}")
    tpl = templates_dir(paths) / f"{role}.md"
    if not tpl.exists():
        raise FileNotFoundError(f"缺少模板 {tpl}")
    c = load_campaign(paths, cid)
    f = c["flows"].get(flow)
    if f is None or not f.get("worktree"):
        raise ValueError(f"流程 {flow} 还没有 worktree，先 `team optimize worktree {flow}`")
    d = flow_dir(paths, cid, flow)
    reviews = _rounds(d, "review")
    rnd = round_ or (reviews[-1][0] + 1 if reviews else 1)
    wt = Path(f["worktree"])
    try:
        head = _git(wt, "rev-parse", "--short", "HEAD")
    except (RuntimeError, OSError):
        head = ""
    prev = next((rv.get("head") for _, rv in reversed(reviews) if rv.get("head")), "") or c["base_commit"]
    bullets = lambda xs: "\n".join(f"- `{x}`" for x in xs) or "- 无"  # noqa: E731
    w = c["window"]
    values = {
        "flow": flow, "campaign": cid, "window": f"{w['start']} ~ {w['end']}（{w['label']}）",
        "worktree": str(wt), "branch": f.get("branch") or "", "base_commit": c["base_commit"],
        "repo_root": str(paths.root), "flow_dir": str(d), "head": head, "prev_head": prev, "round": str(rnd),
        "evidence_file": str(campaign_dir(paths, cid) / "evidence" / f"{flow}.md"),
        "jobs": "、".join(f["jobs"]), "allowed_list": bullets(f.get("allowed", []) + f.get("extra_allowed", [])),
        "shared_list": "\n".join(f"- `{a}`：{', '.join(o)}" for a, o in f.get("shared_agents", {}).items()) or "- 无",
        "requests_list": "\n".join(f"- `{a}`：归 {o}" for a, o in f.get("requests_agents", {}).items()) or "- 无",
    }
    text = tpl.read_text(encoding="utf-8").split("\n", 1)[1].lstrip("\n")   # 去掉给编排者看的标题行
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ---------------- finish：汇总结果、写历史、出报告 ----------------

def flow_dir(paths: Paths, cid: str, flow: str) -> Path:
    return campaign_dir(paths, cid) / "flows" / flow


def _rounds(d: Path, prefix: str) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for p in d.glob(f"{prefix}-r*.json"):
        try:
            n = int(p.stem.rsplit("-r", 1)[1])
        except ValueError:
            continue
        data = read_json_or(p, None)
        if isinstance(data, dict):
            out.append((n, data))
    return sorted(out)


def _diffstat(root: Path, base: str, head: str) -> dict[str, Any]:
    if not base or not head:
        return {"files": [], "insertions": 0, "deletions": 0}
    try:
        raw = _git(root, "diff", "--numstat", base, head)
    except (RuntimeError, OSError):
        return {"files": [], "insertions": 0, "deletions": 0}
    files = []
    for line in raw.splitlines():
        ins, dele, path = (line.split("\t", 2) + ["", ""])[:3]
        files.append({"path": path, "insertions": int(ins) if ins.isdigit() else 0,
                      "deletions": int(dele) if dele.isdigit() else 0})
    return {"files": files, "insertions": sum(f["insertions"] for f in files),
            "deletions": sum(f["deletions"] for f in files)}


def flow_result(paths: Paths, c: dict[str, Any], f: dict[str, Any]) -> dict[str, Any]:
    d = flow_dir(paths, c["campaign_id"], f["name"])
    opt = read_json_or(d / "optimize.json", {}) or {}
    changes = []
    for ch in opt.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        key = str(ch.get("metric") or "")
        # 基线一律按 campaign 证据现算，不用优化者自报的数，保证下次对比口径一致
        changes.append({**ch, "metric": key, "metric_valid": valid_metric(key) if key else False,
                        "baseline": metric_value(f["metrics"], key) if key else None,
                        "direction_label": DIRECTIONS.get(ch.get("direction", ""), ch.get("direction", ""))})
    findings: dict[str, dict[str, Any]] = {}
    reviews = _rounds(d, "review")
    for n, rv in reviews:   # 复审会带上一轮的发现并更新状态：同标题以最新一轮为准
        for it in rv.get("findings") or []:
            if isinstance(it, dict):
                key = str(it.get("title", "")).strip() or f"r{n}-{len(findings)}"
                findings[key] = {**findings.get(key, {}), **it, "round": findings.get(key, {}).get("round", n),
                                 "last_round": n}
    gates = _rounds(d, "gate")
    head = f.get("head") or (gates[-1][1].get("head") if gates else "") or (f["commits"][-1] if f.get("commits") else "")
    return {**{k: f.get(k) for k in ("name", "status", "reason", "score", "plan", "jobs", "agents",
                                     "owned_agents", "shared_agents", "requests_agents", "branch", "commits",
                                     "merge_commit", "review_rounds", "fix_rounds", "verdict", "metrics",
                                     "signal_counts")},
            "summary": opt.get("summary", ""), "changes": changes,
            "cross_flow": normalize_cross_flow(opt.get("cross_flow"), f["name"], f.get("requests_agents") or {}),
            "not_done": opt.get("not_done") or [],
            "risks": opt.get("risks") or [], "findings": list(findings.values()),
            "reviews": [{"round": n, "verdict": rv.get("verdict"), "summary": rv.get("summary", "")}
                        for n, rv in reviews],
            "gates": [{"round": n, "ok": g.get("ok"), "failed": [x["name"] for x in g.get("checks", [])
                                                                 if not x.get("ok")]} for n, g in gates],
            "diffstat": _diffstat(paths.root, c.get("base_commit", ""), head)}


def build_result(paths: Paths, cid: str) -> dict[str, Any]:
    c = load_campaign(paths, cid)
    flows = [flow_result(paths, c, f) for f in c["flows"].values()]
    order = {s: i for i, s in enumerate(("merged", "approved", "abandoned", "gate_failed", "changes_requested"))}
    flows.sort(key=lambda f: (order.get(f["status"], 9), -(f["score"] or 0), f["name"]))
    ids = c.get("framework_issue_ids") or []
    backlog = {it["id"]: it for it in retro._load_backlog(paths)["items"]}
    issues = [{**backlog[i], "score": retro.score(backlog[i])} for i in ids if i in backlog]
    count = lambda st: sum(f["status"] == st for f in flows)  # noqa: E731
    return {"campaign_id": cid, "window": c["window"], "created_at": c["created_at"],
            "finished_at": c.get("finished_at"), "base_commit": c.get("base_commit"),
            "base_branch": c.get("base_branch"), "main_commit": (c.get("main_gate") or {}).get("commit"),
            "main_gate": c.get("main_gate"),
            "totals": {"jobs": sum(len(f["jobs"]) for f in flows), "flows": len(flows),
                       "selected": len(c.get("selected", [])), "merged": count("merged"),
                       "abandoned": count("abandoned"), "skipped": count("skipped"),
                       "changes": sum(len(f["changes"]) for f in flows), "framework_issues": len(issues),
                       # 与下次 collect 的 pending_cross_flow 同口径：按 id 去重，同 campaign 内已闭环的不算
                       "cross_flow_open": len(cross_flow_ledger([{"campaign_id": cid,
                                                                  "flows": {f["name"]: f for f in flows}}])[0])},
            "flows": flows, "framework_candidates": c.get("framework_candidates", []),
            "framework_issues": issues, "previous_effect": c.get("previous_effect", []),
            "pending_cross_flow": c.get("pending_cross_flow", []),
            "timeline": read_log(paths, cid), "signal_labels": SIGNAL_LABELS}


def _history_entry(res: dict[str, Any]) -> dict[str, Any]:
    return {"campaign_id": res["campaign_id"],
            "window": {k: res["window"][k] for k in ("label", "start", "end")},
            "finished_at": res["finished_at"], "base_commit": res["base_commit"],
            "main_commit": res["main_commit"],
            "flows": {f["name"]: {"status": f["status"], "jobs": len(f["jobs"]),
                                  "wall_p50_s": f["metrics"]["wall_p50_s"],
                                  "success_rate": f["metrics"]["success_rate"],
                                  "changes": [{k: ch.get(k) for k in ("title", "direction", "metric",
                                                                      "baseline", "expected")}
                                              for ch in f["changes"]],
                                  # 归一化后的跨流程请求：open/deferred 供下次 collect 提示，done/rejected 用来闭环同 id
                                  "cross_flow": f["cross_flow"]}
                      for f in res["flows"] if f["status"] != "skipped"},
            "framework_issue_ids": [i["id"] for i in res["framework_issues"]]}


def finish(paths: Paths, cid: str) -> dict[str, Any]:
    """收尾：汇总 result.json、写入优化历史（同 campaign 覆盖）、渲染 report.html。可重复执行。"""
    from .optimize_report import render
    with file_lock(_campaign_file(paths, cid)):
        c = load_campaign(paths, cid)
        c["finished_at"] = round(time.time(), 3)
        save_campaign(paths, c)
    log(paths, cid, "campaign_finished")
    res = build_result(paths, cid)
    d = campaign_dir(paths, cid)
    atomic_write_json(d / "result.json", res)
    hf = history_file(paths)
    with file_lock(hf):
        h = load_history(paths)
        h["campaigns"] = [x for x in h["campaigns"] if x.get("campaign_id") != cid] + [_history_entry(res)]
        atomic_write_json(hf, h)
    (d / "report.html").write_text(render(res), encoding="utf-8")
    return res
