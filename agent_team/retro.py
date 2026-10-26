"""流程复盘（自我增强）：Job 收敛后自动拉一个复盘 Job 分析执行过程，优化建议沉淀进待办池。

  sweep / spawn  收敛 Job → 生成 retro-<goal_id>-<时刻> 复盘 Job（每次收敛只生成一次，复盘 Job 自身不再复盘）
  ingest         把复盘产出的 suggestions.json 并入 coordinator/insights/backlog.json（去重、累计复现次数）
  ranked / mark  供 `team insights` 与 team-evolve skill 挑选建议、标记已采纳/已驳回

两级开关，**都打开**才自动复盘，默认全关：
  - 全局：team.yaml 顶层 `retro.enabled`（默认 false）——总闸；
  - 流程：plan 顶层 `"retro": true`（默认 false）——载入时落到 board.retro，历史 board 无此字段即关。
另需存在声明 `flow-retro` 能力的 agent，否则不复盘（不造一个永远 waiting_for_agent 的 Job）。
复盘本身也是普通 Job，走同一套分派/监控/回收。手动 `team retro <goal_id>` 不受两级开关约束。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import ledger, registry, reporting
from .board import Board
from .config import Paths, atomic_write_json, atomic_write_yaml, file_lock, load_team, read_json_or
from .models import DONE, Task

RETRO_PREFIX = "retro-"
RETRO_TASK = "analyze-run"
RETRO_CAP = "flow-retro"
SUGGESTIONS_FILE = "suggestions.json"
STATUSES = ("open", "applied", "dismissed")
_IMPACT = {"high": 3.0, "medium": 2.0, "low": 1.0}
_EFFORT = {"low": 0.5, "medium": 0.0, "high": -0.5}


def _state_file(paths: Paths) -> Path:
    """sweep 去重账本（运行时状态，随 coordinator/state 不入库）。"""
    return paths.state_dir / "retro.json"


def backlog_file(paths: Paths) -> Path:
    """建议待办池（项目知识，入库，跨会话/跨人共享）。"""
    return paths.root / "coordinator" / "insights" / "backlog.json"


def is_retro(goal_id: str) -> bool:
    return goal_id.startswith(RETRO_PREFIX)


def globally_enabled(paths: Paths) -> bool:
    """全局总闸 team.yaml `retro.enabled`，默认关。"""
    cfg = load_team(paths).get("retro") or {}
    return isinstance(cfg, dict) and cfg.get("enabled") is True


def set_global(paths: Paths, on: bool) -> None:
    with file_lock(paths.team_file):
        team = load_team(paths)
        cfg = team.get("retro") if isinstance(team.get("retro"), dict) else {}
        cfg.pop("auto", None)
        cfg["enabled"] = on
        team["retro"] = cfg
        atomic_write_yaml(paths.team_file, team)


_PLAN_FLAG_RE = re.compile(r'^(\s*)"retro"\s*:\s*(true|false)\s*(,?)\s*$', re.M)


def set_plan_flag(plan_file: Path, on: bool) -> None:
    """改 plan 模板顶层 `retro`。模板是手工排版的 JSON，只做行级改写不整份重排；改完回读校验。"""
    text = plan_file.read_text(encoding="utf-8")
    val = "true" if on else "false"
    m = _PLAN_FLAG_RE.search(text)
    if m:
        new = text[:m.start()] + f'{m.group(1)}"retro": {val}{m.group(3)}' + text[m.end():]
    else:
        brace = text.index("{")
        new = text[:brace + 1] + f'\n  "retro": {val},' + text[brace + 1:]
    data = json.loads(new)
    if not isinstance(data, dict) or data.get("retro") is not on:   # 命中了嵌套的同名键等意外：退回整份重写
        data = json.loads(text)
        data["retro"] = on
        new = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    plan_file.write_text(new, encoding="utf-8")


def has_retro_agent(paths: Paths) -> bool:
    return any(RETRO_CAP in m.capabilities for m in registry.list_agents(paths))


def _has_real_runs(board: Board) -> bool:
    """Job 里至少有一次真实（非 dry-run）执行——全 dry-run / 全跳过的 Job 没有可复盘的过程。"""
    return any(not r.get("dry_run") for t in board.tasks.values() for r in t.runs)


def _completion_key(board: Board) -> str:
    # 同一 goal_id 可被 plan 重载再跑：按「哪次收敛」去重，重跑收敛会再复盘一次
    return f"{board.goal_id}@{board.completed_at:.3f}"


def sweep(paths: Paths) -> list[str]:
    """为新收敛的 Job 生成复盘 Job，返回新建的复盘 goal_id。全局扫、幂等（锁内去重），可在任意驱动点调用。"""
    if not globally_enabled(paths) or not has_retro_agent(paths):
        return []
    spawned: list[str] = []
    sf = _state_file(paths)
    with file_lock(sf):
        st = read_json_or(sf, {})
        now = time.time()
        done = st.setdefault("spawned", {})
        for b in Board.load_all(paths):
            if (not b.retro or is_retro(b.goal_id) or not b.is_complete() or b.completed_at is None
                    or not _has_real_runs(b)):
                continue
            key = _completion_key(b)
            if key in done:
                continue
            rgid = spawn(paths, b, reason="auto")
            done[key] = {"retro_goal_id": rgid, "ts": round(now, 3)}
            spawned.append(rgid)
        atomic_write_json(sf, st)
    return spawned


def _match_templates(paths: Paths, board: Board) -> list[Path]:
    """找出本 Job 实例化自哪个 plan 模板：模板 task_id 集合 == Job 的非 fan-out 子任务集合。

    board 不记模板来源（plan --as 会改 goal_id），按 DAG 骨架对齐最稳；同 goal_id 的排前。"""
    base = {tid for tid, t in board.tasks.items() if not t.fanout_group}
    hits: list[tuple[int, Path]] = []
    for f in sorted(paths.plans_dir.glob("*.json")):
        data = read_json_or(f, {})
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            continue
        if {t.get("task_id") for t in data["tasks"] if isinstance(t, dict)} == base:
            hits.append((0 if data.get("goal_id") == board.goal_id else 1, f))
    return [f for _, f in sorted(hits)]


def _write_inputs(paths: Paths, board: Board, rgid: str) -> Path:
    """把复盘要读的材料预先落盘：Job 报告（含计时/打回/失败）+ 本 Job 的调度事件流。

    run.log 是全 Job 混写的大文件，预先按 goal_id 过滤，免得复盘 worker 自己 grep 还读漏。"""
    d = paths.job_artifacts_dir(rgid) / "_input"
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(d / "report.json", reporting.report(paths, board.goal_id))
    events: list[str] = []
    log_file = paths.state_dir / "run.log"
    if log_file.exists():
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                try:
                    if json.loads(line).get("goal_id") == board.goal_id:
                        events.append(line.rstrip("\n"))
                except json.JSONDecodeError:
                    continue
    (d / "events.jsonl").write_text("\n".join(events) + ("\n" if events else ""), encoding="utf-8")
    return d


def _retro_goal(paths: Paths, board: Board, input_dir: Path) -> str:
    gid = board.goal_id
    agents = sorted({r.get("agent") for t in board.tasks.values() for r in t.runs if r.get("agent")}
                    | {t.agent for t in board.tasks.values() if t.agent})
    tpls = _match_templates(paths, board)
    root = paths.root
    lines = [
        f"复盘已收敛的 Job `{gid}`（目标：{board.goal or '—'}；结果：{board.summary()}），"
        "分析这次执行过程，给出「本流程」与「整个项目」两个层面、有证据支撑的优化建议。",
        "",
        "材料（只读）：",
        f"- Job 报告（任务状态/摘要/产物/计时/打回/失败）：{input_dir / 'report.json'}",
        f"- 本 Job 调度事件流（JSONL：dispatched/task_done/task_failed/task_timeout/task_rejected/task_retasked…）：{input_dir / 'events.jsonl'}",
        f"- Job 看板原始状态（含每次运行记录 runs）：{paths.job_file(gid)}",
        f"- Job 产物目录：{paths.job_artifacts_dir(gid)}",
        "- plan 模板：" + ("、".join(str(p) for p in tpls) if tpls else "未匹配到模板（可能是临时 plan）"),
        "- 参与的 agent persona：" + ("、".join(str(paths.agent_dir(a) / 'prompt.md') for a in agents) or "—"),
        f"- 框架协议与代码：{root / 'coordinator' / 'prompt.md'}、{root / 'coordinator' / 'routing.md'}、{root / 'agent_team'}",
        f"- 已有建议池（避免重复，复现的旧问题用 dup_of 指向原 id）：{backlog_file(paths)}",
        "",
        f"产出：artifacts_dir/{SUGGESTIONS_FILE}，JSON 对象，字段：source_goal_id、verdict（一句话评价这次执行）、"
        "suggestions（数组）。每条建议字段：title（一句话）、scope（flow=本流程/plan/相关 agent，project=框架/协议/通用）、"
        "category（plan/agent-prompt/routing/framework/skill/reliability/cost/docs 之一）、target（要改的文件路径或组件）、"
        "problem（观察到的问题）、evidence（数组，事件/文件:行/摘要原文等可核对的证据）、proposal（具体改法）、"
        "impact（high/medium/low）、effort（high/medium/low）、confidence（0~1）、dup_of（复现已有建议时填其 id，否则 null）。",
        "执行顺利、没有值得改的就给空 suggestions，不要凑数。",
    ]
    return "\n".join(lines)


def spawn(paths: Paths, board: Board, reason: str = "auto") -> str:
    """为一个已收敛 Job 建复盘 Job（只建不派，派发由调用方的分派轮负责），返回复盘 goal_id。"""
    stamp = time.strftime("%m%d%H%M%S", time.localtime(board.completed_at or time.time()))
    rgid = f"{RETRO_PREFIX}{board.goal_id}-{stamp}"
    input_dir = _write_inputs(paths, board, rgid)
    task = Task(task_id=RETRO_TASK, goal=_retro_goal(paths, board, input_dir),
                capabilities=[RETRO_CAP], max_retries=1,
                expect=f"写 artifacts_dir/{SUGGESTIONS_FILE} 后把结果写 outbox，summary 写建议条数与最重要的一条")
    rb = Board(rgid, f"复盘 {board.goal_id}", [task])
    rb.validate()
    rb.refresh_ready()
    rb.save(paths)
    ledger.log_event(paths, "retro_spawned", goal_id=board.goal_id, retro_goal_id=rgid, reason=reason)
    return rgid


def record_manual(paths: Paths, board: Board, rgid: str) -> None:
    """手动 `team retro` 也记账，免得之后 sweep 对同一次收敛再复盘一遍。"""
    if board.completed_at is None:
        return
    sf = _state_file(paths)
    with file_lock(sf):
        st = read_json_or(sf, {})
        st.setdefault("spawned", {})[_completion_key(board)] = {
            "retro_goal_id": rgid, "ts": round(time.time(), 3), "manual": True}
        atomic_write_json(sf, st)


# ---------------- 建议待办池 ----------------

def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _fingerprint(s: dict[str, Any]) -> str:
    raw = "|".join(_norm(s.get(k)) for k in ("scope", "target", "title"))
    return "ins-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _pick(v: Any, allowed: dict[str, float], default: str) -> str:
    v = _norm(v)
    return v if v in allowed else default


def score(item: dict[str, Any]) -> float:
    """排序分：影响×置信 + 复现加成（同一问题多次复盘都出现=真痛点）+ 改动量加减。"""
    conf = item.get("confidence")
    conf = min(max(float(conf), 0.0), 1.0) if isinstance(conf, (int, float)) else 0.6
    recur = min(0.5 * (int(item.get("occurrences", 1)) - 1), 2.0)
    return round(_IMPACT[item["impact"]] * conf + recur + _EFFORT[item["effort"]], 2)


def _load_backlog(paths: Paths) -> dict[str, Any]:
    data = read_json_or(backlog_file(paths), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("ingested", {})
    data.setdefault("items", [])
    return data


def _suggestions_path(paths: Paths, board: Board) -> Path | None:
    task = board.tasks.get(RETRO_TASK)
    if task is None or task.status != DONE:
        return None
    cands = [Path(a) if Path(a).is_absolute() else paths.root / a
             for a in task.artifacts if str(a).endswith(SUGGESTIONS_FILE)]
    cands.append(paths.task_artifacts_dir(board.goal_id, RETRO_TASK) / SUGGESTIONS_FILE)
    return next((p for p in cands if p.is_file()), None)


def _merge(items: list[dict[str, Any]], s: dict[str, Any], source: str, now: float) -> str:
    """把一条建议并入池：命中已有（dup_of 或指纹相同）就累计复现，否则新增。返回 new/recurred/reopened。"""
    by_id = {it["id"]: it for it in items}
    hit = by_id.get(str(s.get("dup_of") or "")) or by_id.get(_fingerprint(s))
    if hit is not None:
        if source not in hit["sources"]:
            hit["sources"].append(source)
            hit["occurrences"] += 1
        hit["evidence"] = (hit["evidence"] + [e for e in s.get("evidence") or [] if e not in hit["evidence"]])[-10:]
        hit["last_seen"] = now
        if hit["status"] == "applied":       # 已采纳的改动没治好：重新打开等下一轮处理
            hit["status"] = "open"
            hit["note"] = (hit.get("note", "") + f"；采纳后在 {source} 复现，已重新打开").lstrip("；")
            return "reopened"
        return "recurred"
    items.append({
        "id": _fingerprint(s), "title": str(s.get("title", "")).strip(),
        "scope": "flow" if _norm(s.get("scope")) == "flow" else "project",
        "category": _norm(s.get("category")) or "other", "target": str(s.get("target") or ""),
        "problem": str(s.get("problem") or ""), "proposal": str(s.get("proposal") or ""),
        "evidence": [e for e in s.get("evidence") or [] if isinstance(e, str)][:10],
        "impact": _pick(s.get("impact"), _IMPACT, "medium"),
        "effort": _pick(s.get("effort"), _EFFORT, "medium"),
        "confidence": s.get("confidence") if isinstance(s.get("confidence"), (int, float)) else None,
        "status": "open", "occurrences": 1, "sources": [source],
        "first_seen": now, "last_seen": now, "note": ""})
    return "new"


def ingest(paths: Paths) -> dict[str, Any]:
    """把所有已完成复盘 Job 的 suggestions.json 并入待办池；按文件 mtime 记账，重复调用幂等。"""
    stats = {"new": 0, "recurred": 0, "reopened": 0, "sources": [], "malformed": []}
    cands = [(b, sp) for b in Board.load_all(paths)
             if is_retro(b.goal_id) and (sp := _suggestions_path(paths, b)) is not None]
    if not cands:                        # 每轮 tick 都会调：没有已完成的复盘就别碰池子（不建锁文件）
        return stats
    bf = backlog_file(paths)
    with file_lock(bf):
        data = _load_backlog(paths)
        for b, sp in cands:
            mtime = sp.stat().st_mtime
            if data["ingested"].get(b.goal_id) == mtime:
                continue
            raw = read_json_or(sp, None)
            sugs = raw.get("suggestions") if isinstance(raw, dict) else raw
            if not isinstance(sugs, list):
                stats["malformed"].append(str(sp))
                continue
            source = (raw.get("source_goal_id") if isinstance(raw, dict) else None) or b.goal_id
            now = round(time.time(), 3)
            for s in sugs:
                if isinstance(s, dict) and str(s.get("title", "")).strip():
                    stats[_merge(data["items"], s, str(source), now)] += 1
            data["ingested"][b.goal_id] = mtime
            stats["sources"].append(b.goal_id)
        if stats["sources"]:
            atomic_write_json(bf, data)
    return stats


def ranked(paths: Paths, status: str | None = "open", scope: str | None = None,
           top: int | None = None) -> list[dict[str, Any]]:
    items = [dict(it, score=score(it)) for it in _load_backlog(paths)["items"]
             if (not status or it["status"] == status) and (not scope or it["scope"] == scope)]
    items.sort(key=lambda it: (-it["score"], -it["last_seen"]))
    return items[:top] if top else items


def mark(paths: Paths, ids: list[str], status: str, note: str = "") -> list[str]:
    """改建议状态（applied=已落地、dismissed=不采纳、open=重开）；返回未找到的 id。"""
    if status not in STATUSES:
        raise ValueError(f"status 只能是 {STATUSES}，得到 {status!r}")
    bf = backlog_file(paths)
    with file_lock(bf):
        data = _load_backlog(paths)
        by_id = {it["id"]: it for it in data["items"]}
        missing = [i for i in ids if i not in by_id]
        for i in ids:
            if i in by_id:
                by_id[i]["status"] = status
                by_id[i]["updated_at"] = round(time.time(), 3)
                if note:
                    by_id[i]["note"] = note
        if len(missing) < len(ids):
            atomic_write_json(bf, data)
    return missing
