"""建议池批量落地（team-evolve）：选题 → 合并分析分组 → 每组 worktree 独立开发/评审 → 合并 main。

编排（派分析/开发/评审 subagent）由 `.claude/skills/team-evolve` 驱动；本模块只做确定性部分：
  start       从建议池选 open 建议，建 run、落证据
  group       校验分析 agent 的 analysis.json（每条建议恰好分配/驳回、组间文件集不相交、无环依赖）
  log/record  只追加的 evolve-log.jsonl；状态类事件同步推进 run.json
  finish      汇总 result.json/report.md，并把落地/驳回结果写回建议池
worktree / 门禁 / 合并见 evolve_ops，通用门禁原语见 gate。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import optimize as opt, retro
from .config import Paths, atomic_write_json, file_lock, read_json_or

MAX_INSIGHTS = 12          # 单次 run 最多选多少条建议
MAX_GROUPS = 6             # 分析后最多分多少组（同时派发的开发 agent 上限即此）
MAX_FIX_ROUNDS = 2         # 评审 FAIL 后最多修复几轮
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GROUP_STATUSES = ("selected", "optimizing", "optimized", "gated", "gate_failed", "reviewing",
                  "changes_requested", "fixing", "approved", "merged", "abandoned")
_STATUS_BY_EVENT = {"worktree_created": "optimizing", "developer_done": "optimized", "fix_done": "optimized",
                    "review_started": "reviewing", "fix_round": "fixing", "merged": "merged",
                    "abandoned": "abandoned"}
# 组不得改运行时状态/产物/建议池本身/其他 worktree：这些不属于任何一条建议的落地范围。
FORBIDDEN_GROUP_PREFIXES = ("coordinator/state/", "coordinator/insights/", "shared/", ".worktrees/")


# ---------------- 路径与小工具 ----------------

def runs_root(paths: Paths) -> Path:
    """run 明细（证据/交付/评审/日志）：运行时数据，随 shared/artifacts 不入库。"""
    return paths.artifacts_dir / "evolve"


def run_dir(paths: Paths, rid: str) -> Path:
    return runs_root(paths) / rid


def run_file(paths: Paths, rid: str) -> Path:
    return run_dir(paths, rid) / "run.json"


def group_dir(paths: Paths, rid: str, group: str) -> Path:
    return run_dir(paths, rid) / "groups" / group


def worktrees_root(paths: Paths, rid: str) -> Path:
    return paths.root / ".worktrees" / rid


def branch_name(rid: str, group: str) -> str:
    return f"evolve/{rid}/{group}"


def new_run_id(paths: Paths) -> str:
    base = f"evolve-{time.strftime('%Y%m%d')}"
    rid, n = base, 2
    while run_dir(paths, rid).exists():
        rid, n = f"{base}-{n}", n + 1
    return rid


def load_run(paths: Paths, rid: str) -> dict[str, Any]:
    c = read_json_or(run_file(paths, rid), None)
    if not isinstance(c, dict):
        raise FileNotFoundError(f"evolve run {rid} 不存在或已损坏：{run_file(paths, rid)}")
    return c


def save_run(paths: Paths, c: dict[str, Any]) -> None:
    c["updated_at"] = round(time.time(), 3)
    atomic_write_json(run_file(paths, c["run_id"]), c)


def latest_run(paths: Paths) -> str:
    root = runs_root(paths)
    cands = sorted(root.glob("*/run.json"), key=lambda p: p.stat().st_mtime) if root.exists() else []
    if not cands:
        raise FileNotFoundError("还没有 evolve run，先跑 `team evolve start`")
    return cands[-1].parent.name


def _norm_path(p: Any) -> str:
    return str(p or "").strip().strip("/")


def _paths_overlap(a: str, b: str) -> bool:
    """文件相等，或一方是另一方的目录前缀，都算占用同一片文件。"""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _valid_group_path(p: str) -> bool:
    if not p or p.startswith("/") or ".." in p.split("/"):
        return False
    if p.startswith(FORBIDDEN_GROUP_PREFIXES):
        return False
    # 过宽的目录前缀（如 coordinator/）会间接放行运行时状态，也拒绝
    return not any(f.startswith(p + "/") for f in FORBIDDEN_GROUP_PREFIXES)


# ---------------- run 与日志 ----------------

def log(paths: Paths, rid: str, event: str, group: str = "", **fields: Any) -> dict[str, Any]:
    """追加一条 evolve 日志；状态类事件同步推进 run.json 里该组的状态。"""
    ev = {"ts": round(time.time(), 3), "event": event, **({"group": group} if group else {}), **fields}
    d = run_dir(paths, rid)
    with file_lock(run_file(paths, rid)):
        c = load_run(paths, rid)
        g = c["groups"].get(group) if group else None
        if group and g is None:
            raise KeyError(f"run {rid} 里没有组 {group}")
        if g is not None:
            if event == "fix_round" and g.get("fix_rounds", 0) >= MAX_FIX_ROUNDS:
                raise ValueError(f"{group} 已修复 {MAX_FIX_ROUNDS} 轮仍未通过评审，按准则应 abandoned，不再修")
            if event in _STATUS_BY_EVENT:
                g["status"] = _STATUS_BY_EVENT[event]
            if event == "worktree_created":
                g["worktree"], g["branch"] = fields.get("worktree"), fields.get("branch")
            elif event in ("developer_done", "fix_done") and fields.get("commit"):
                g.setdefault("commits", []).append(fields["commit"])
            elif event == "gate_result":
                g["gate"] = {k: fields.get(k) for k in ("ok", "round", "file")}
                g["status"] = "gated" if fields.get("ok") else "gate_failed"
            elif event == "review_verdict":
                g["review_rounds"] = int(fields.get("round") or g.get("review_rounds", 0) + 1)
                g["verdict"] = fields.get("verdict")
                g["status"] = "approved" if fields.get("verdict") == "PASS" else "changes_requested"
            elif event == "fix_round":
                g["fix_rounds"] = g.get("fix_rounds", 0) + 1
            elif event == "merged":
                g["merge_commit"], g["head"] = fields.get("commit"), fields.get("head")
                if fields.get("commits"):
                    g["commits"] = list(fields["commits"])
            elif event == "abandoned":
                g["reason"] = fields.get("reason", g.get("reason", ""))
        elif event == "main_gate":
            c["main_gate"] = {k: fields.get(k) for k in ("ok", "file", "commit")}
        with open(d / "evolve-log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        save_run(paths, c)
    return ev


def read_log(paths: Paths, rid: str) -> list[dict[str, Any]]:
    f = run_dir(paths, rid) / "evolve-log.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------- start：选题、落证据 ----------------

def select_insights(paths: Paths, ids: list[str] | None = None, top: int = MAX_INSIGHTS) -> list[dict[str, Any]]:
    """取 open 建议：给了 ids 就按 ids（必须都是 open），否则按得分取前 top 条。"""
    retro.ingest(paths)                                  # 先把已完成复盘的新建议收进来
    open_items = {it["id"]: it for it in retro.ranked(paths, status="open")}
    if ids:
        missing = [i for i in ids if i not in open_items]
        if missing:
            raise ValueError(f"这些建议不存在或不是 open：{', '.join(missing)}")
        items = [open_items[i] for i in ids]
    else:
        items = retro.ranked(paths, status="open", top=top)
    if not items:
        raise ValueError("建议池没有 open 建议可落地")
    if len(items) > MAX_INSIGHTS:
        raise ValueError(f"一次最多选 {MAX_INSIGHTS} 条建议，当前 {len(items)} 条")
    return items


def _insight_brief(it: dict[str, Any]) -> dict[str, Any]:
    return {k: it.get(k) for k in ("id", "title", "scope", "category", "target", "problem", "proposal",
                                   "evidence", "impact", "effort", "score", "occurrences")}


def _evidence_markdown(rid: str, items: list[dict[str, Any]]) -> str:
    lines = [f"# evolve {rid} 选中建议（{len(items)} 条）", ""]
    for it in items:
        lines += [f"## [{it['id']}] {it['title']}", "",
                  f"- scope/category：{it.get('scope')}/{it.get('category')}；影响 {it.get('impact')}，"
                  f"改动 {it.get('effort')}，得分 {it.get('score')}，复现 {it.get('occurrences')}",
                  f"- target：{it.get('target') or '—'}", "",
                  f"**问题**：{it.get('problem') or '—'}", "",
                  f"**建议**：{it.get('proposal') or '—'}", ""]
        ev = it.get("evidence") or []
        lines += ["**证据**："] + ([f"- {x}" for x in ev] or ["- 无"]) + [""]
    return "\n".join(lines)


def _group_markdown(g: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = [f"# 组 {g['name']}：{g['title']}", "", f"**计划**：{g['plan'] or '—'}", "",
             "## 涉及建议", ""]
    for it in items:
        lines += [f"### [{it['id']}] {it['title']}", "",
                  f"- target：{it.get('target') or '—'}", "",
                  f"**问题**：{it.get('problem') or '—'}", "",
                  f"**建议**：{it.get('proposal') or '—'}", ""]
        ev = it.get("evidence") or []
        lines += ["**证据**："] + ([f"- {x}" for x in ev] or ["- 无"]) + [""]
    lines += ["## 允许改动的文件（门禁会校验，越界即失败）", ""]
    lines += [f"- `{p}`" for p in g["allowed"]]
    return "\n".join(lines) + "\n"


def start(paths: Paths, ids: list[str] | None = None, top: int = MAX_INSIGHTS, run_id: str = "") -> dict[str, Any]:
    """建 run：选 open 建议、记基线、写证据。要求主工作区干净（合并回 main 时才安全）。"""
    rid = run_id or new_run_id(paths)
    d = run_dir(paths, rid)
    if (d / "run.json").exists():
        raise FileExistsError(f"evolve run {rid} 已存在：{d}")
    dirty = opt._git(paths.root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError(f"主工作区有未提交改动，先处理再开始：\n{dirty}")
    items = select_insights(paths, list(dict.fromkeys(ids)) if ids else None, top)
    base = opt._git(paths.root, "rev-parse", "--short", "HEAD")
    branch = opt._git(paths.root, "rev-parse", "--abbrev-ref", "HEAD")
    (d / "evidence").mkdir(parents=True, exist_ok=True)
    c = {"run_id": rid, "created_at": round(time.time(), 3), "status": "started",
         "base_commit": base, "base_branch": branch,
         "insight_ids": [it["id"] for it in items], "insights": [_insight_brief(it) for it in items],
         "dismissed_proposals": [], "merges": [], "groups": {}, "main_gate": None}
    atomic_write_json(d / "evidence" / "insights.json", items)
    (d / "evidence" / "insights.md").write_text(_evidence_markdown(rid, items), encoding="utf-8")
    save_run(paths, c)
    log(paths, rid, "run_started", base_commit=base, insights=c["insight_ids"])
    return load_run(paths, rid)


# ---------------- group：校验并落分析结果 ----------------

def _resolve_merges(selected: set[str], merges: list[Any]) -> dict[str, str]:
    """校验 merges 并把每个合并源解析到最终主建议；返回 {source: root}（不含 source==root）。"""
    into: dict[str, str] = {}
    for m in merges:
        if not isinstance(m, dict):
            raise ValueError("merges 的每一项必须是对象")
        srcs = [str(x) for x in (m.get("insight_ids") or [])]
        dest = str(m.get("into") or "")
        if not srcs or not dest:
            raise ValueError("merges 每项都要有非空 insight_ids 与 into")
        for x in srcs + [dest]:
            if x not in selected:
                raise ValueError(f"merges 引用了本次未选中的建议 {x}")
        for x in srcs:
            if x == dest:
                raise ValueError(f"merges 不能把 {x} 合并到它自己")
            if into.get(x, dest) != dest:
                raise ValueError(f"建议 {x} 被合并到多个目标")
            into[x] = dest
    resolved: dict[str, str] = {}
    for src in into:
        seen, cur = set(), src
        while cur in into:
            if cur in seen:
                raise ValueError(f"merges 存在环：{src}")
            seen.add(cur)
            cur = into[cur]
        resolved[src] = cur
    return resolved


def _check_acyclic(groups: dict[str, dict[str, Any]]) -> None:
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 1:
            raise ValueError(f"depends_on 存在环：{' -> '.join(path + [n])}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for dep in groups[n].get("depends_on") or []:
            visit(dep, path + [n])
        state[n] = 2

    for n in groups:
        visit(n, [])


def validate_analysis(run: dict[str, Any], analysis: Any) -> dict[str, Any]:
    """校验 analysis.json 并归一化成 run.groups / dismissed_proposals / merges。失败抛 ValueError。"""
    if not isinstance(analysis, dict):
        raise ValueError("analysis.json 必须是对象")
    selected = set(run.get("insight_ids") or [])
    merges = _resolve_merges(selected, analysis.get("merges") or [])
    dismissed: list[dict[str, str]] = []
    seen_dismissed: set[str] = set()
    for it in analysis.get("dismissed") or []:
        if not isinstance(it, dict):
            raise ValueError("dismissed 的每一项必须是对象")
        iid, reason = str(it.get("insight_id") or ""), str(it.get("reason") or "").strip()
        if iid not in selected:
            raise ValueError(f"dismissed 引用了本次未选中的建议 {iid!r}")
        if not reason:
            raise ValueError(f"dismissed 的 {iid} 没写理由")
        if iid in seen_dismissed:
            raise ValueError(f"dismissed 重复：{iid}")
        seen_dismissed.add(iid)
        dismissed.append({"insight_id": iid, "reason": reason})

    raw_groups = analysis.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("groups 必须是非空数组")
    if len(raw_groups) > MAX_GROUPS:
        raise ValueError(f"最多分 {MAX_GROUPS} 组，当前 {len(raw_groups)} 组")
    groups: dict[str, dict[str, Any]] = {}
    assigned: dict[str, str] = {}
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("groups 的每一项必须是对象")
        name = str(raw.get("name") or "").strip()
        if not SLUG_RE.match(name):
            raise ValueError(f"组名 {name!r} 不是 slug（小写字母/数字/连字符，且不以连字符开头）")
        if name in groups:
            raise ValueError(f"组名重复：{name}")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValueError(f"组 {name} 缺 title")
        ids = [str(x) for x in (raw.get("insight_ids") or [])]
        if not ids:
            raise ValueError(f"组 {name} 没有 insight_ids")
        for iid in ids:
            if iid not in selected:
                raise ValueError(f"组 {name} 引用了本次未选中的建议 {iid}")
            if iid in assigned:
                raise ValueError(f"建议 {iid} 同时被分到 {assigned[iid]} 和 {name}")
            if iid in seen_dismissed:
                raise ValueError(f"建议 {iid} 既被驳回又被分到组 {name}")
            assigned[iid] = name
        files = [_norm_path(x) for x in (raw.get("files") or []) if _norm_path(x)]
        if not files:
            raise ValueError(f"组 {name} 的 files 不能为空")
        bad = [x for x in files if not _valid_group_path(x)]
        if bad:
            raise ValueError(f"组 {name} 的 files 含非法路径：{bad}")
        deps = [str(x) for x in (raw.get("depends_on") or [])]
        groups[name] = {"name": name, "title": title, "plan": str(raw.get("plan") or ""),
                        "insight_ids": ids, "allowed": files, "depends_on": deps,
                        "status": "selected", "worktree": None, "branch": None, "commits": [],
                        "gate": None, "review_rounds": 0, "fix_rounds": 0, "verdict": None,
                        "reason": "", "merge_commit": None}
    names = set(groups)
    for n, g in groups.items():
        for dep in g["depends_on"]:
            if dep == n:
                raise ValueError(f"组 {n} 不能依赖自己")
            if dep not in names:
                raise ValueError(f"组 {n} 的 depends_on 引用了不存在的组 {dep}")
    _check_acyclic(groups)
    for a_name, a in groups.items():
        for b_name, b in groups.items():
            if a_name >= b_name:
                continue
            for x in a["allowed"]:
                for y in b["allowed"]:
                    if _paths_overlap(x, y):
                        raise ValueError(f"组 {a_name} 与 {b_name} 的改动文件相交（{x} / {y}），不能独立开发")
    for iid in selected:
        if iid in seen_dismissed or iid in assigned:
            continue
        if merges.get(iid) and merges[iid] in assigned:
            continue
        raise ValueError(f"建议 {iid} 没有分配到任何组，也没有被驳回")
    for iid in seen_dismissed:
        if iid in merges:
            raise ValueError(f"建议 {iid} 不能既被驳回又被合并")
    for src, root in merges.items():
        if root in seen_dismissed:
            raise ValueError(f"建议 {src} 合并到 {root}，但 {root} 被驳回；应一并驳回或改指向其他建议")
        if src in assigned and assigned.get(root) and assigned[src] != assigned[root]:
            raise ValueError(f"建议 {src} 与合并目标 {root} 被分到不同组（{assigned[src]} / {assigned[root]}），应放同一组")
    return {"groups": groups, "dismissed": dismissed,
            "merges": [m for m in (analysis.get("merges") or []) if isinstance(m, dict)],
            "summary": str(analysis.get("summary") or "")}


def apply_analysis(paths: Paths, rid: str, analysis_path: Path) -> dict[str, Any]:
    """读取分析结果、校验、落 run.json 与每组证据文件；失败不动 run。"""
    analysis = read_json_or(analysis_path, None)
    c = load_run(paths, rid)
    if c.get("groups"):
        raise ValueError(f"run {rid} 已经分组过，不能重复应用分析结果")
    norm = validate_analysis(c, analysis)
    d = run_dir(paths, rid)
    atomic_write_json(d / "analysis.json", analysis)
    by_id = {it["id"]: it for it in c.get("insights") or []}
    for name, g in norm["groups"].items():
        gd = group_dir(paths, rid, name)
        gd.mkdir(parents=True, exist_ok=True)
        items = [by_id[i] for i in g["insight_ids"] if i in by_id]
        (d / "evidence" / f"{name}.md").write_text(_group_markdown(g, items), encoding="utf-8")
    c["groups"] = norm["groups"]
    c["dismissed_proposals"] = norm["dismissed"]
    c["merges"] = norm["merges"]
    c["analysis_summary"] = norm["summary"]
    c["status"] = "grouped"
    save_run(paths, c)
    log(paths, rid, "groups_created", groups=sorted(norm["groups"]),
        dismissed=[x["insight_id"] for x in norm["dismissed"]])
    return load_run(paths, rid)


# ---------------- 记录交付与评审 ----------------

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


def record_developer(paths: Paths, rid: str, group: str) -> dict[str, Any]:
    """开发 agent 交付后调用：读 worktree HEAD 与 report.json，记 developer_done（修复轮记 fix_done）。"""
    c = load_run(paths, rid)
    g = c["groups"].get(group)
    if g is None:
        raise KeyError(f"run {rid} 里没有组 {group}")
    if not g.get("worktree"):
        raise ValueError(f"组 {group} 还没有 worktree，先 `team evolve worktree {group}`")
    head = opt._git(Path(g["worktree"]), "rev-parse", "--short", "HEAD")
    data = read_json_or(group_dir(paths, rid, group) / "report.json", {}) or {}
    event = "fix_done" if g.get("fix_rounds") else "developer_done"
    return log(paths, rid, event, group=group, commit=head, changes=len(data.get("changes") or []))


def record_review(paths: Paths, rid: str, group: str) -> dict[str, Any]:
    """评审 agent 交付后调用：取最新一轮 review-r<N>.json，校验后记 review_verdict。"""
    reviews = _rounds(group_dir(paths, rid, group), "review")
    if not reviews:
        raise FileNotFoundError(f"组 {group} 还没有 review-r<N>.json")
    n, rv = reviews[-1]
    verdict = str(rv.get("verdict", "")).upper()
    if verdict not in ("PASS", "FAIL"):
        raise ValueError(f"review-r{n}.json 的 verdict 必须是 PASS/FAIL，得到 {rv.get('verdict')!r}")
    findings = [x for x in rv.get("findings") or [] if isinstance(x, dict)]
    blocking = [x for x in findings if x.get("severity") == "blocking" and x.get("status") != "fixed"]
    if verdict == "PASS" and blocking:
        raise ValueError(f"review-r{n}.json 判 PASS 却仍有 {len(blocking)} 条未修复的 blocking 问题")
    return log(paths, rid, "review_verdict", group=group, round=n, verdict=verdict,
               blocking=len(blocking), findings=len(findings))


# ---------------- subagent 提示词 ----------------

def templates_dir(paths: Paths) -> Path:
    return paths.root / ".claude" / "skills" / "team-evolve" / "templates"


def _bullets(xs: list[str]) -> str:
    return "\n".join(f"- `{x}`" for x in xs) or "- 无"


def render_prompt(paths: Paths, rid: str, role: str, group: str = "", round_: int = 0) -> str:
    """把 skill 里的分析/开发/评审模板填成可直接派发的提示词。"""
    if role not in ("analyzer", "developer", "reviewer"):
        raise ValueError(f"role 只能是 analyzer/developer/reviewer，得到 {role!r}")
    tpl = templates_dir(paths) / f"{role}.md"
    if not tpl.exists():
        raise FileNotFoundError(f"缺少模板 {tpl}")
    c = load_run(paths, rid)
    d = run_dir(paths, rid)
    values = {
        "run": rid, "repo_root": str(paths.root), "base_commit": c["base_commit"],
        "base_branch": c["base_branch"], "insights_file": str(d / "evidence" / "insights.md"),
        "insights_json": str(d / "evidence" / "insights.json"), "run_dir": str(d),
        "analysis_file": str(d / "analysis.json"), "selected_ids": "、".join(c.get("insight_ids") or []),
        "max_groups": str(MAX_GROUPS), "max_insights": str(MAX_INSIGHTS),
    }
    if role != "analyzer":
        g = c["groups"].get(group)
        if g is None:
            raise KeyError(f"run {rid} 里没有组 {group}")
        if role == "developer" and not g.get("worktree"):
            raise ValueError(f"组 {group} 还没有 worktree，先 `team evolve worktree {group}`")
        gd = group_dir(paths, rid, group)
        reviews = _rounds(gd, "review")
        rnd = round_ or (reviews[-1][0] + 1 if reviews else 1)
        wt = Path(g["worktree"]) if g.get("worktree") else Path("")
        try:
            head = opt._git(wt, "rev-parse", "--short", "HEAD") if g.get("worktree") else ""
        except (RuntimeError, OSError):
            head = ""
        prev = next((rv.get("head") for _, rv in reversed(reviews) if rv.get("head")), "") or c["base_commit"]
        values |= {
            "group": group, "group_title": g["title"], "group_plan": g["plan"] or "—",
            "worktree": str(wt), "branch": g.get("branch") or "", "group_dir": str(gd),
            "report_file": str(gd / "report.json"), "evidence_file": str(d / "evidence" / f"{group}.md"),
            "allowed_list": _bullets(g.get("allowed") or []), "head": head, "prev_head": prev,
            "round": str(rnd), "insight_ids": "、".join(g.get("insight_ids") or []),
            "depends_on": "、".join(g.get("depends_on") or []) or "无",
        }
    text = tpl.read_text(encoding="utf-8").split("\n", 1)[1].lstrip("\n")   # 去掉给编排者看的标题行
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ---------------- finish：汇总结果、回写建议池 ----------------

def _group_result(paths: Paths, rid: str, g: dict[str, Any]) -> dict[str, Any]:
    d = group_dir(paths, rid, g["name"])
    rep = read_json_or(d / "report.json", {}) or {}
    reviews = _rounds(d, "review")
    gates = _rounds(d, "gate")
    return {"name": g["name"], "title": g["title"], "status": g["status"], "plan": g["plan"],
            "insight_ids": g["insight_ids"], "allowed": g["allowed"], "depends_on": g["depends_on"],
            "worktree": g.get("worktree"), "branch": g.get("branch"), "commits": g.get("commits") or [],
            "merge_commit": g.get("merge_commit"), "review_rounds": g.get("review_rounds", 0),
            "fix_rounds": g.get("fix_rounds", 0), "verdict": g.get("verdict"),
            "reason": g.get("reason", ""), "summary": rep.get("summary", ""),
            "changes": rep.get("changes") or [], "not_done": rep.get("not_done") or [],
            "risks": rep.get("risks") or [],
            "reviews": [{"round": n, "verdict": rv.get("verdict"), "summary": rv.get("summary", "")}
                        for n, rv in reviews],
            "gates": [{"round": n, "ok": x.get("ok"),
                       "failed": [c["name"] for c in x.get("checks", []) if not c.get("ok")]}
                      for n, x in gates]}


def build_result(paths: Paths, rid: str) -> dict[str, Any]:
    c = load_run(paths, rid)
    groups = [_group_result(paths, rid, g) for g in c["groups"].values()]
    order = {s: i for i, s in enumerate(("merged", "approved", "abandoned", "gate_failed", "changes_requested"))}
    groups.sort(key=lambda g: (order.get(g["status"], 9), g["name"]))
    count = lambda st: sum(g["status"] == st for g in groups)  # noqa: E731
    return {"run_id": rid, "created_at": c.get("created_at"), "finished_at": c.get("finished_at"),
            "base_commit": c.get("base_commit"), "base_branch": c.get("base_branch"),
            "main_gate": c.get("main_gate"), "status": c.get("status"),
            "totals": {"insights": len(c.get("insight_ids") or []), "groups": len(groups),
                       "merged": count("merged"), "abandoned": count("abandoned"),
                       "changes": sum(len(g["changes"]) for g in groups),
                       "dismissed": len(c.get("dismissed_proposals") or [])},
            "groups": groups, "dismissed_proposals": c.get("dismissed_proposals") or [],
            "merges": c.get("merges") or [], "timeline": read_log(paths, rid)}


def _report_markdown(res: dict[str, Any]) -> str:
    t = res["totals"]
    lines = [f"# evolve {res['run_id']} 报告", "",
             f"- 基线：`{res['base_commit']}`（{res['base_branch']}）",
             f"- 选中建议 {t['insights']} 条，分 {t['groups']} 组，合并 {t['merged']} 组，放弃 {t['abandoned']} 组",
             f"- 改动 {t['changes']} 条，驳回建议 {t['dismissed']} 条", "",
             "## 各组", "", "| 组 | 状态 | 建议 | 评审 | 修复轮 | 合并 commit |", "|---|---|---|---|---|---|"]
    for g in res["groups"]:
        lines.append(f"| {g['name']} | {g['status']} | {'、'.join(g['insight_ids'])} | "
                     f"{g['verdict'] or '—'} | {g['fix_rounds']} | {g['merge_commit'] or '—'} |")
    if res["dismissed_proposals"]:
        lines += ["", "## 分析阶段驳回", ""]
        lines += [f"- `{x['insight_id']}`：{x['reason']}" for x in res["dismissed_proposals"]]
    if res["merges"]:
        lines += ["", "## 合并分析", ""]
        lines += [f"- {'、'.join(m.get('insight_ids') or [])} → `{m.get('into')}`：{m.get('reason') or '—'}"
                  for m in res["merges"]]
    if res.get("main_gate"):
        mg = res["main_gate"]
        lines += ["", f"## 主分支门禁：{'通过' if mg.get('ok') else '失败'}（{mg.get('commit') or '—'}）"]
    return "\n".join(lines) + "\n"


def finish(paths: Paths, rid: str, dry_run: bool = False) -> dict[str, Any]:
    """收尾：汇总结果、写 report.md，并把落地/驳回结果回写建议池。可重复执行。"""
    c = load_run(paths, rid)
    res = build_result(paths, rid)
    applied: list[str] = []
    dismissed: list[str] = []
    merged_ids: set[str] = set()
    for g in c["groups"].values():
        if g["status"] == "merged":
            merged_ids |= set(g["insight_ids"])
    # 合并分析里的重复建议：主建议落地则源建议按「已合并」驳回
    for m in c.get("merges") or []:
        dest = str(m.get("into") or "")
        if dest in merged_ids:
            for src in m.get("insight_ids") or []:
                if str(src) != dest and str(src) not in merged_ids:
                    dismissed.append((str(src), f"与 {dest} 合并（{rid}）"))
    for g in c["groups"].values():
        if g["status"] != "merged":
            continue
        for iid in g["insight_ids"]:
            if iid in merged_ids:
                applied.append((iid, f"已由 {rid}/{g['name']} 落地（merge {g.get('merge_commit') or '—'}）"))
    for x in c.get("dismissed_proposals") or []:
        dismissed.append((x["insight_id"], f"分析阶段驳回（{rid}）：{x['reason']}"))
    res["applied_ids"] = [i for i, _ in applied]
    res["dismissed_ids"] = [i for i, _ in dismissed]
    if dry_run:
        return res
    for iid, note in applied:
        retro.mark(paths, [iid], "applied", note=note)
    for iid, note in dismissed:
        retro.mark(paths, [iid], "dismissed", note=note)
    # 未落地的组：建议保持 open，只在仍是 open 时补一条说明，方便下次继续
    open_now = {it["id"] for it in retro.ranked(paths, status="open")}
    for g in c["groups"].values():
        if g["status"] in ("merged", "selected", "optimizing"):
            continue
        for iid in g["insight_ids"]:
            if iid in open_now:
                retro.mark(paths, [iid], "open",
                           note=f"{rid}/{g['name']} 未落地（{g['status']}）：{g.get('reason') or '—'}")
    c = load_run(paths, rid)
    c["status"] = "done"
    c["finished_at"] = round(time.time(), 3)
    save_run(paths, c)
    log(paths, rid, "run_finished", applied=len(applied), dismissed=len(dismissed))
    res = build_result(paths, rid)
    res["applied_ids"] = [i for i, _ in applied]
    res["dismissed_ids"] = [i for i, _ in dismissed]
    d = run_dir(paths, rid)
    atomic_write_json(d / "result.json", res)
    (d / "report.md").write_text(_report_markdown(res), encoding="utf-8")
    return res
