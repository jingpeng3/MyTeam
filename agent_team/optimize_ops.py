"""流程优化 campaign 的 git 侧操作：每流程一个 worktree、门禁检查（边界 + 质量）、合并回主分支并清理。

门禁是代码检查，不靠 prompt 自觉：改动文件必须在该流程白名单内、工作区已提交、commit 前缀正确、
JSON 合法、全部 plan 可解析且 DAG/占位槽自洽、pytest/ruff 全绿、无空白错误；worktree 模式还校验
优化者交付的 optimize.json（每条改动有方向、证据与合法验证指标）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import gate
from . import optimize as opt
from .config import Paths, atomic_write_json, read_json_or

COMMIT_PREFIX_RE = r"^(optimize|fix)\({flow}\): "
SLOW_CHECKS = gate.SLOW_CHECKS

# 兼容旧引用：门禁原语已抽到 gate.py，本模块内部与外部调用保持原名。
_run = gate.run_cmd
_check = gate.check
_changed_files = gate.changed_files
_commit_subjects = gate.commit_subjects
plan_problems = gate.plan_problems


# ---------------- worktree ----------------

def create_worktree(paths: Paths, cid: str, flow: str) -> dict[str, Any]:
    """从 campaign 基线提交拉出 `.worktrees/<cid>/<flow>`（分支 opt/<cid>/<flow>）。已存在则直接复用。"""
    c = opt.load_campaign(paths, cid)
    f = c["flows"].get(flow)
    if f is None or f["status"] == "skipped":
        raise ValueError(f"流程 {flow} 不在 campaign {cid} 的入选清单里")
    wt = opt.worktrees_root(paths, cid) / flow
    br = opt.branch_name(cid, flow)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        opt._git(paths.root, "worktree", "add", "-q", "-b", br, str(wt), c["base_commit"] or "HEAD")
    opt.flow_dir(paths, cid, flow).mkdir(parents=True, exist_ok=True)
    opt.log(paths, cid, "worktree_created", flow=flow, worktree=str(wt), branch=br)
    return {"flow": flow, "worktree": str(wt), "branch": br, "base_commit": c["base_commit"],
            "evidence": str(opt.campaign_dir(paths, cid) / "evidence" / f"{flow}.md"),
            "flow_dir": str(opt.flow_dir(paths, cid, flow))}


# ---------------- 门禁 ----------------

# 优化者判断「本流程已无可改进项」时的显式空交付标记：changes 为空是合法的，但必须自证不是漏写。
_NOOP_STATUSES = ("noop", "abandoned", "skipped")


def report_is_noop(data: Any) -> bool:
    """optimize.json 是否显式声明「无可改进项」（noop=true 或 status=noop/abandoned/skipped）。"""
    if not isinstance(data, dict):
        return False
    return data.get("noop") is True or str(data.get("status") or "").strip().lower() in _NOOP_STATUSES


def optimize_report_problems(path: Path) -> list[str]:
    """优化者交付契约：每条改动要有标题、合法方向、证据、改动文件与词表内的验证指标；cross_flow 轻校验。

    显式 noop（noop=true / status=noop）允许 changes 为空——编排协议允许优化者判定
    「plan 层已无可改进项」并交付空改动、由编排者标 abandoned；此时仍要求 summary 说明理由，
    否则空交付无法与漏写区分。"""
    data = read_json_or(path, None)
    if not isinstance(data, dict):
        return [f"缺少或无法解析 {path}"]
    out = []
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        if report_is_noop(data):
            if not str(data.get("summary") or "").strip():
                out.append("noop 交付需在 summary 写明「无可改进项」的理由")
            return out + cross_flow_problems(data.get("cross_flow"))
        out.append("changes 为空：没有改动就不该进入门禁（显式 noop=true / status=noop 或标 abandoned/skipped）")
        changes = []
    for i, ch in enumerate(changes):
        tag = f"changes[{i}]"
        if not isinstance(ch, dict):
            out.append(f"{tag} 不是对象")
            continue
        if not str(ch.get("title", "")).strip():
            out.append(f"{tag} 缺 title")
        if ch.get("direction") not in opt.DIRECTIONS:
            out.append(f"{tag} direction={ch.get('direction')!r} 不在 {sorted(opt.DIRECTIONS)}")
        if not ch.get("evidence"):
            out.append(f"{tag} 没有证据（evidence 为空）")
        if not ch.get("files"):
            out.append(f"{tag} 没写改了哪些文件（files）")
        if not opt.valid_metric(str(ch.get("metric") or "")):
            out.append(f"{tag} 验证指标 {ch.get('metric')!r} 不在词表（team optimize metrics 查看）")
    return out + cross_flow_problems(data.get("cross_flow"))


def cross_flow_problems(items: Any) -> list[str]:
    """cross_flow 轻校验：必须是列表；对象项 request 非空、status 在取值内。字符串项是旧交付，照收。"""
    if items is None:
        return []
    if not isinstance(items, list):
        return [f"cross_flow 必须是数组，得到 {type(items).__name__}"]
    out = []
    for i, it in enumerate(items):
        tag = f"cross_flow[{i}]"
        if isinstance(it, str):
            continue
        if not isinstance(it, dict):
            out.append(f"{tag} 必须是对象（或旧格式字符串）")
            continue
        if not str(it.get("request") or "").strip():
            out.append(f"{tag} 缺 request")
        st = str(it.get("status") or "").strip().lower()      # 与 normalize_cross_flow 同样按小写比较
        if st and st not in opt.CROSS_FLOW_STATUSES:
            out.append(f"{tag} status={it.get('status')!r} 不在 {list(opt.CROSS_FLOW_STATUSES)}")
    return out


def run_gate(root: Path, *, base: str, flow: dict[str, Any] | None = None,
             report_path: Path | None = None, skip_slow: bool = False) -> dict[str, Any]:
    """在 root（worktree 或主仓）跑门禁。flow 给定=worktree 模式，额外校验边界、commit 前缀与优化报告。"""
    checks: list[dict[str, Any]] = []
    status = opt._git(root, "status", "--porcelain")
    checks.append(_check("clean", not status, status.splitlines()[:20]))
    changed = _changed_files(root, base) if base else []
    # noop 交付（显式声明无可改进项）没有 commit/改动：门禁只为验证交付格式，不应被 has_changes 卡死。
    noop = report_path is not None and report_is_noop(read_json_or(report_path, None))
    if flow is not None:
        name = flow["name"]
        out_of_scope = [p for p in changed if not opt.allowed(flow, p)]
        checks.append(_check("scope", not out_of_scope,
                             {"out_of_scope": out_of_scope, "allowed": flow.get("allowed", [])
                              + flow.get("extra_allowed", [])}))
        checks.append(_check("has_changes", bool(changed) or noop, {"changed": changed, "noop": noop}))
        pat = re.compile(COMMIT_PREFIX_RE.format(flow=re.escape(name)))
        subjects = _commit_subjects(root, base)
        bad = [s for s in subjects if not pat.match(s)]
        checks.append(_check("commit_prefix", noop or bool(subjects and not bad),
                             {"bad": bad, "expect": f"optimize({name}): … 或 fix({name}): …"}))
        if report_path is not None:
            probs = optimize_report_problems(report_path)
            checks.append(_check("optimize_report", not probs, probs))
    checks += gate.common_checks(root, base, changed, skip_slow=skip_slow)
    return {"ok": all(x["ok"] for x in checks), "noop": noop, "root": str(root), "base": base,
            "head": opt._git(root, "rev-parse", "--short", "HEAD"), "changed": changed,
            "checks": checks, "skipped": list(SLOW_CHECKS) if skip_slow else [], "ts": round(time.time(), 3)}


def gate_flow(paths: Paths, cid: str, flow: str, skip_slow: bool = False) -> dict[str, Any]:
    """worktree 门禁：结果落 flows/<flow>/gate-r<N>.json（N 自增），并记日志推进状态。"""
    c = opt.load_campaign(paths, cid)
    f = c["flows"].get(flow)
    if f is None or not f.get("worktree"):
        raise ValueError(f"流程 {flow} 还没有 worktree，先 `team optimize worktree {flow}`")
    d = opt.flow_dir(paths, cid, flow)
    res = run_gate(Path(f["worktree"]), base=c["base_commit"], flow=f,
                   report_path=d / "optimize.json", skip_slow=skip_slow)
    n = len(list(d.glob("gate-r*.json"))) + 1
    out = d / f"gate-r{n}.json"
    atomic_write_json(out, res)
    opt.log(paths, cid, "gate_result", flow=flow, ok=res["ok"], round=n, file=str(out), head=res["head"],
            failed=[x["name"] for x in res["checks"] if not x["ok"]])
    return {**res, "file": str(out), "round": n}


# ---------------- 合并 ----------------

def _branch_commits(root: Path, base: str, branch: str) -> list[str]:
    raw = opt._git(root, "log", "--no-merges", "--format=%h", f"{base}..{branch}")
    return list(reversed([x for x in raw.splitlines() if x.strip()]))


def merge(paths: Paths, cid: str, keep_worktrees: bool = False, skip_slow: bool = False) -> dict[str, Any]:
    """把评审通过（approved）且门禁通过的流程依次 --no-ff 合入主分支，再对主分支跑全量门禁。

    主仓工作区必须干净、当前分支必须是 campaign 基线分支。合并冲突立即 abort 并停下（不自动解冲突）。"""
    c = opt.load_campaign(paths, cid)
    root = paths.root
    cur = opt._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if c.get("base_branch") and cur != c["base_branch"]:
        raise RuntimeError(f"当前分支 {cur} 不是 campaign 基线分支 {c['base_branch']}，先切回再合并")
    dirty = opt._git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError(f"主仓有未提交改动，合并前先处理：\n{dirty}")
    merged, conflict = [], None
    for name, f in sorted(c["flows"].items()):
        if f["status"] != "approved":
            continue
        if not (f.get("gate") or {}).get("ok"):
            opt.log(paths, cid, "abandoned", flow=name, reason="评审通过但最近一次门禁未通过")
            continue
        br = f["branch"]
        head = opt._git(root, "rev-parse", "--short", br)
        commits = _branch_commits(root, c["base_commit"], br)
        msg = (f"merge: 合入 {name} 流程优化（{cid}，worktree + 独立评审通过）\n\n"
               "Co-Authored-By: Claude Code <noreply@anthropic.com>")
        ok, out = _run(["git", "merge", "--no-ff", br, "-m", msg], root)
        if not ok:
            _run(["git", "merge", "--abort"], root)
            conflict = {"flow": name, "detail": out}
            opt.log(paths, cid, "merge_conflict", flow=name, detail=out[-1000:])
            break
        mc = opt._git(root, "rev-parse", "--short", "HEAD")
        opt.log(paths, cid, "merged", flow=name, commit=mc, head=head, commits=commits)
        merged.append(name)
    gate = None
    if merged and conflict is None:
        gate = run_gate(root, base=c["base_commit"], skip_slow=skip_slow)
        out_file = opt.campaign_dir(paths, cid) / "main-gate.json"
        atomic_write_json(out_file, gate)
        opt.log(paths, cid, "main_gate", ok=gate["ok"], file=str(out_file), commit=gate["head"])
    cleaned = [] if keep_worktrees or conflict else cleanup(paths, cid)
    return {"merged": merged, "conflict": conflict, "main_gate": gate, "cleaned": cleaned}


def cleanup(paths: Paths, cid: str) -> list[str]:
    """移除本 campaign 的全部 worktree；已合并流程的分支一并删除，未合并的分支保留备查。"""
    c = opt.load_campaign(paths, cid)
    done = []
    for f in c["flows"].values():
        wt = f.get("worktree")
        if wt and Path(wt).exists():
            _run(["git", "worktree", "remove", "--force", wt], paths.root)
            done.append(wt)
        if f["status"] == "merged" and f.get("branch"):
            _run(["git", "branch", "-d", f["branch"]], paths.root)
    root = opt.worktrees_root(paths, cid)
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
    _run(["git", "worktree", "prune"], paths.root)
    return done
