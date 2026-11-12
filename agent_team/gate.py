"""git 侧门禁原语：命令执行、改动清单、plan 自洽检查与通用质量检查。

optimize（流程优化）与 evolve（建议池批量落地）共用这一层；各自再叠加自己的边界/交付契约检查。
这里只做确定性检查，不依赖任何 campaign 结构，也不写任何状态。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import optimize as opt
from .board import Board, DagError
from .models import PLACEHOLDER_RE, Task

SLOW_CHECKS = ("pytest", "ruff")


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 600) -> tuple[bool, str]:
    """跑一条命令，返回 (成功?, 合并后的输出尾部)。异常也按失败处理，不向上抛。"""
    import subprocess
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
    return p.returncode == 0, out[-4000:]


def check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def changed_files(root: Path, base: str) -> list[str]:
    if not base:
        return []
    raw = opt._git(root, "diff", "--name-only", f"{base}..HEAD")
    return [x for x in raw.splitlines() if x.strip()]


def commit_subjects(root: Path, base: str) -> list[str]:
    raw = opt._git(root, "log", "--no-merges", "--format=%s", f"{base}..HEAD")
    return [x for x in raw.splitlines() if x.strip()]


def plan_problems(root: Path) -> list[str]:
    """全部 plan：可解析、任务字段合法、DAG 自洽、占位槽与 routing.required_slots 一致。"""
    problems = []
    for f in sorted((root / "coordinator" / "plans").glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            problems.append(f"{f.name}: JSON 非法 {e}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            continue
        try:
            tasks = [Task.from_dict({**t, "goal": t.get("goal") or t.get("instruction") or t.get("title")})
                     for t in data["tasks"]]
            Board(data.get("goal_id", f.stem), data.get("goal", ""), tasks).validate()
        except (DagError, ValueError, TypeError) as e:
            problems.append(f"{f.name}: {e}")
            continue
        req = (data.get("routing") or {}).get("required_slots")
        if req is None:
            continue
        fan = {v for t in data["tasks"] if t.get("fanout")
               for v in (t["fanout"].get("shard_var", "shard"), t["fanout"].get("total_var", "total"))}
        body = json.dumps({k: v for k, v in data.items() if k != "routing"}, ensure_ascii=False)
        slots = set(PLACEHOLDER_RE.findall(body)) - fan
        if slots != set(req):
            problems.append(f"{f.name}: 占位槽 {sorted(slots)} 与 required_slots {sorted(req)} 不一致")
    return problems


def common_checks(root: Path, base: str, changed: list[str], *, skip_slow: bool = False) -> list[dict[str, Any]]:
    """两种 worktree 门禁共用的检查：JSON 合法、plan 自洽、空白错误、pytest、ruff。"""
    checks: list[dict[str, Any]] = []
    bad_json = []
    for rel in changed:
        p = root / rel
        if rel.endswith(".json") and p.exists():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except ValueError as e:
                bad_json.append(f"{rel}: {e}")
    checks.append(check("json", not bad_json, bad_json))
    probs = plan_problems(root)
    checks.append(check("plans", not probs, probs))
    if base:
        ok, out = run_cmd(["git", "diff", "--check", f"{base}..HEAD"], root)
        checks.append(check("diff_check", ok, out))
    if not skip_slow:
        ok, out = run_cmd(["uv", "run", "--extra", "dev", "pytest", "-q"], root)
        checks.append(check("pytest", ok, out.splitlines()[-3:]))
        ok, out = run_cmd(["uv", "run", "--extra", "dev", "ruff", "check"], root)
        checks.append(check("ruff", ok, out.splitlines()[-3:]))
    return checks
