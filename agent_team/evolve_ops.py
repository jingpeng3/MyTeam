"""evolve 的 git 侧操作：每组一个 worktree、门禁（边界 + 交付契约 + 质量）、合并回主分支并清理。

与 optimize 的区别：evolve 落地的是建议池条目，允许改框架/测试/协议/skill；边界来自分析阶段给每组
声明的 `files`，门禁保证改动不越出该集合，从而各组分支文件集互不相交、可独立评审与无冲突合并。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import evolve, gate, optimize as opt
from .config import Paths, atomic_write_json, read_json_or

COMMIT_PREFIX_RE = r"^(evolve|fix)\({group}\): "


# ---------------- worktree ----------------

def create_worktree(paths: Paths, rid: str, group: str) -> dict[str, Any]:
    """从 run 基线拉出 `.worktrees/<rid>/<group>`（分支 evolve/<rid>/<group>）。已存在则直接复用。"""
    c = evolve.load_run(paths, rid)
    g = c["groups"].get(group)
    if g is None:
        raise ValueError(f"run {rid} 里没有组 {group}")
    wt = evolve.worktrees_root(paths, rid) / group
    br = evolve.branch_name(rid, group)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        if opt._git(paths.root, "branch", "--list", br):
            opt._git(paths.root, "worktree", "add", "-q", str(wt), br)          # 复用上次未合并的分支
        else:
            opt._git(paths.root, "worktree", "add", "-q", "-b", br, str(wt), c["base_commit"] or "HEAD")
    evolve.group_dir(paths, rid, group).mkdir(parents=True, exist_ok=True)
    evolve.log(paths, rid, "worktree_created", group=group, worktree=str(wt), branch=br)
    return {"group": group, "worktree": str(wt), "branch": br, "base_commit": c["base_commit"],
            "evidence": str(evolve.run_dir(paths, rid) / "evidence" / f"{group}.md"),
            "group_dir": str(evolve.group_dir(paths, rid, group))}


# ---------------- 门禁 ----------------

def _in_scope(allowed: list[str], rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in allowed)


def report_problems(path: Path, group: dict[str, Any]) -> list[str]:
    """开发交付契约：每条 change 要覆盖组内建议，文件不越界，且必须写清证据与验证方式。"""
    data = read_json_or(path, None)
    if not isinstance(data, dict):
        return [f"缺少或无法解析 {path}"]
    out: list[str] = []
    if not str(data.get("summary") or "").strip():
        out.append("缺 summary：写清本组解决了哪些建议")
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        out.append("changes 为空：本组必须至少有一条改动（整组没做应在分析阶段驳回）")
        return out
    group_ids = set(group.get("insight_ids") or [])
    allowed = group.get("allowed") or []
    covered: set[str] = set()
    for i, ch in enumerate(changes):
        tag = f"changes[{i}]"
        if not isinstance(ch, dict):
            out.append(f"{tag} 不是对象")
            continue
        for key in ("title", "change", "verification"):
            if not str(ch.get(key) or "").strip():
                out.append(f"{tag} 缺 {key}")
        if not ch.get("evidence"):
            out.append(f"{tag} 没有证据（evidence 为空）")
        ids = [str(x) for x in (ch.get("insight_ids") or [])]
        if not ids:
            out.append(f"{tag} 没写 insight_ids")
        extra = [x for x in ids if x not in group_ids]
        if extra:
            out.append(f"{tag} 引用了不属于本组的建议：{extra}")
        covered |= set(ids)
        files = [str(x) for x in (ch.get("files") or [])]
        if not files:
            out.append(f"{tag} 没写改了哪些文件（files）")
        bad = [x for x in files if not _in_scope(allowed, x)]
        if bad:
            out.append(f"{tag} 改了组外文件：{bad}")
    missing = sorted(group_ids - covered)
    if missing:
        out.append(f"组内建议未被任何改动覆盖：{missing}")
    return out


def run_gate(root: Path, *, base: str, group: dict[str, Any], report_path: Path,
             skip_slow: bool = False) -> dict[str, Any]:
    """单组门禁：干净、边界、有改动、commit 前缀、交付契约，再叠加通用质量检查。"""
    checks: list[dict[str, Any]] = []
    status = opt._git(root, "status", "--porcelain")
    checks.append(gate.check("clean", not status, status.splitlines()[:20]))
    changed = gate.changed_files(root, base) if base else []
    out_of_scope = [p for p in changed if not _in_scope(group.get("allowed") or [], p)]
    checks.append(gate.check("scope", not out_of_scope,
                             {"out_of_scope": out_of_scope, "allowed": group.get("allowed") or []}))
    checks.append(gate.check("has_changes", bool(changed), changed))
    pat = re.compile(COMMIT_PREFIX_RE.format(group=re.escape(group["name"])))
    subjects = gate.commit_subjects(root, base)
    bad = [s for s in subjects if not pat.match(s)]
    checks.append(gate.check("commit_prefix", bool(subjects and not bad),
                             {"bad": bad, "expect": f"evolve({group['name']}): … 或 fix({group['name']}): …"}))
    probs = report_problems(report_path, group)
    checks.append(gate.check("report_contract", not probs, probs))
    checks += gate.common_checks(root, base, changed, skip_slow=skip_slow)
    return {"ok": all(x["ok"] for x in checks), "root": str(root), "base": base,
            "head": opt._git(root, "rev-parse", "--short", "HEAD"), "changed": changed,
            "checks": checks, "skipped": list(gate.SLOW_CHECKS) if skip_slow else [],
            "ts": round(time.time(), 3)}


def gate_group(paths: Paths, rid: str, group: str, *, skip_slow: bool = False,
               preview: bool = False) -> dict[str, Any]:
    """对某组 worktree 跑门禁。preview=True 只返回结果，不落 gate-r<N>.json、不推进状态。"""
    c = evolve.load_run(paths, rid)
    g = c["groups"].get(group)
    if g is None or not g.get("worktree"):
        raise ValueError(f"组 {group} 还没有 worktree，先 `team evolve worktree {group}`")
    d = evolve.group_dir(paths, rid, group)
    d.mkdir(parents=True, exist_ok=True)
    res = run_gate(Path(g["worktree"]), base=c["base_commit"], group=g,
                   report_path=d / "report.json", skip_slow=skip_slow)
    n = len(list(d.glob("gate-r*.json"))) + (0 if preview else 1)
    out = {**res, "round": n, "file": "" if preview else str(d / f"gate-r{n}.json")}
    if not preview:
        atomic_write_json(Path(out["file"]), res)
        evolve.log(paths, rid, "gate_result", group=group, ok=res["ok"], round=n, file=out["file"],
                   head=res["head"], failed=[x["name"] for x in res["checks"] if not x["ok"]])
    return out


def run_repo_gate(root: Path, base: str, *, skip_slow: bool = False) -> dict[str, Any]:
    """主分支合并后的全量门禁：干净 + 相对基线的改动/空白/测试检查，无组边界。"""
    checks: list[dict[str, Any]] = []
    status = opt._git(root, "status", "--porcelain")
    checks.append(gate.check("clean", not status, status.splitlines()[:20]))
    changed = gate.changed_files(root, base) if base else []
    checks += gate.common_checks(root, base, changed, skip_slow=skip_slow)
    return {"ok": all(x["ok"] for x in checks), "root": str(root), "base": base,
            "head": opt._git(root, "rev-parse", "--short", "HEAD"), "changed": changed,
            "checks": checks, "skipped": list(gate.SLOW_CHECKS) if skip_slow else [],
            "ts": round(time.time(), 3)}


# ---------------- 合并 ----------------

def _topo(groups: dict[str, dict[str, Any]]) -> list[str]:
    """按 depends_on 拓扑排序；同层按组名稳定排序。校验阶段已保证无环。"""
    done: list[str] = []
    pending = set(groups)
    while pending:
        ready = sorted(n for n in pending if not (set(groups[n].get("depends_on") or []) & pending))
        if not ready:                       # 理论上不可达：validate_analysis 已挡环
            ready = sorted(pending)
        for n in ready:
            pending.discard(n)
            done.append(n)
    return done


def _branch_commits(root: Path, base: str, branch: str) -> list[str]:
    raw = opt._git(root, "log", "--no-merges", "--format=%h", f"{base}..{branch}")
    return list(reversed([x for x in raw.splitlines() if x.strip()]))


def merge(paths: Paths, rid: str, keep_worktrees: bool = False, skip_slow: bool = False) -> dict[str, Any]:
    """把 approved 且门禁通过的组按依赖顺序 --no-ff 合入主分支，再跑主分支全量门禁。

    主仓必须干净且处于 run 的基线分支；冲突立即 abort 并停下（不自动解冲突）。"""
    c = evolve.load_run(paths, rid)
    root = paths.root
    cur = opt._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if c.get("base_branch") and cur != c["base_branch"]:
        raise RuntimeError(f"当前分支 {cur} 不是 run 基线分支 {c['base_branch']}，先切回再合并")
    dirty = opt._git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError(f"主仓有未提交改动，合并前先处理：\n{dirty}")
    merged, conflict = [], None
    for name in _topo(c["groups"]):
        f = c["groups"][name]
        if f["status"] != "approved":
            continue
        blocked = [d for d in (f.get("depends_on") or []) if d not in merged]
        if blocked:
            evolve.log(paths, rid, "abandoned", group=name,
                       reason=f"依赖组 {', '.join(blocked)} 未合并，不能单独合入")
            continue
        if not (f.get("gate") or {}).get("ok"):
            evolve.log(paths, rid, "abandoned", group=name, reason="评审通过但最近一次门禁未通过")
            continue
        if not f.get("branch"):
            evolve.log(paths, rid, "abandoned", group=name, reason="评审通过但没有分支（未建 worktree）")
            continue
        br = f["branch"]
        head = opt._git(root, "rev-parse", "--short", br)
        commits = _branch_commits(root, c["base_commit"], br)
        msg = (f"merge: evolve {name} ({rid}，worktree + 独立评审通过)\n\n"
               "Co-Authored-By: Claude Code <noreply@anthropic.com>")
        ok, out = gate.run_cmd(["git", "merge", "--no-ff", br, "-m", msg], root)
        if not ok:
            gate.run_cmd(["git", "merge", "--abort"], root)
            conflict = {"group": name, "detail": out}
            evolve.log(paths, rid, "merge_conflict", group=name, detail=out[-1000:])
            break
        mc = opt._git(root, "rev-parse", "--short", "HEAD")
        evolve.log(paths, rid, "merged", group=name, commit=mc, head=head, commits=commits)
        merged.append(name)
    gate_res = None
    if merged and conflict is None:
        gate_res = run_repo_gate(root, c["base_commit"], skip_slow=skip_slow)
        out_file = evolve.run_dir(paths, rid) / "main-gate.json"
        atomic_write_json(out_file, gate_res)
        evolve.log(paths, rid, "main_gate", ok=gate_res["ok"], file=str(out_file), commit=gate_res["head"])
    cleaned = [] if keep_worktrees or conflict else cleanup(paths, rid)
    return {"merged": merged, "conflict": conflict, "main_gate": gate_res, "cleaned": cleaned}


def cleanup(paths: Paths, rid: str) -> list[str]:
    """移除本 run 的全部 worktree；已合并组的分支删除，未合并分支保留备查。"""
    c = evolve.load_run(paths, rid)
    done = []
    for f in c["groups"].values():
        wt = f.get("worktree")
        if wt and Path(wt).exists():
            gate.run_cmd(["git", "worktree", "remove", "--force", wt], paths.root)
            done.append(wt)
        if f["status"] == "merged" and f.get("branch"):
            gate.run_cmd(["git", "branch", "-d", f["branch"]], paths.root)
    root = evolve.worktrees_root(paths, rid)
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
    gate.run_cmd(["git", "worktree", "prune"], paths.root)
    return done
