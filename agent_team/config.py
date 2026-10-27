"""路径与配置加载。"""
from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import yaml

from .models import AgentManifest, Runtime, _as_list

try:                        # POSIX 有 fcntl；Windows 无，降级为无锁（best-effort）
    import fcntl
except ImportError:         # pragma: no cover - 仅 Windows 命中
    fcntl = None            # type: ignore[assignment]


def _unique_tmp(path: Path) -> Path:
    """每次写一个进程/调用唯一的 .tmp 名。固定 `<target>.tmp` 会让两个未持锁的进程共写同一
    tmp，os.replace 互相搬走对方半截内容而崩（FileNotFoundError）；带 pid+随机则各写各的。"""
    return path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def atomic_write_json(path: Path, data: Any) -> None:
    """原子落盘 JSON：写唯一 .tmp 再 os.replace，避免进程中途崩溃留下半截文件。

    runtime.json / jobs/<goal_id>.json 是回收 pane、恢复进度的唯一账本；崩溃点又常在
    写文件时，非原子写会让「崩溃即损坏」成为常态。与 signals._write_json 统一。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        # 序列化中途抛异常（如传入不可 JSON 化对象）时清掉残留 tmp，不泄漏半截文件
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def atomic_write_yaml(path: Path, data: Any) -> None:
    """原子落盘 YAML：写唯一 .tmp 再 os.replace。

    team.yaml 是 project_root() 的探测锚点、又被并发建/删 agent 读-改-写；
    裸 open("w") 崩溃即留半截文件会拖垮整个项目定位。与 atomic_write_json 同策略。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


@contextlib.contextmanager
def file_lock(target: Path) -> Iterator[None]:
    """对 target 的并发读-改-写加互斥锁（跨进程/跨会话）。

    锁落在 <target>.lock 独立文件上，不锁 target 本身——这样与 os.replace 原子写
    互不干扰（replace 会换掉 inode，锁在 target 上会失效）。fcntl 不可用时降级为无锁。
    """
    if fcntl is None:       # Windows：无 flock，退回无锁（单机单进程仍安全）
        yield
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_json_or(path: Path, default: Any) -> Any:
    """容错读 JSON：文件损坏/缺失返回 default，不让状态文件损坏拖垮整轮调度。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):   # ValueError 涵盖 JSONDecodeError 与半截多字节的 UnicodeDecodeError
        return default


# 本包所在仓库根（editable 安装时即 TEAMS 仓库；普通安装时是 site-packages，不含 team.yaml）
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def project_root() -> Path:
    """项目根：从 CWD 向上找含 team.yaml 的目录；找不到则回退到本包所在仓库（editable 安装下
    即可从任意目录调用 `team`）；仍找不到用 CWD。"""
    p = Path.cwd()
    for cand in [p, *p.parents]:
        if (cand / "team.yaml").exists():
            return cand
    if (_PACKAGE_ROOT / "team.yaml").exists():
        return _PACKAGE_ROOT
    return p


class Paths:
    def __init__(self, root: Path | None = None):
        self.root = root or project_root()

    @property
    def team_file(self) -> Path:
        return self.root / "team.yaml"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def plans_dir(self) -> Path:
        return self.root / "coordinator" / "plans"

    @property
    def state_dir(self) -> Path:
        return self.root / "coordinator" / "state"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def runtime_file(self) -> Path:
        """runtime 账本：`<goal_id>:<task_id>` → 在途 pane 映射，回收 pane 的唯一依据。"""
        return self.state_dir / "runtime.json"

    def job_file(self, goal_id: str) -> Path:
        return self.jobs_dir / f"{goal_id}.json"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "shared" / "artifacts"

    def job_artifacts_dir(self, goal_id: str) -> Path:
        """每个 Job 独立的共享产物目录：shared/artifacts/<goal_id>/。"""
        return self.artifacts_dir / goal_id

    def task_artifacts_dir(self, goal_id: str, task_id: str) -> Path:
        """每个任务的产物「当前」目录：shared/artifacts/<goal_id>/<task_id>/。

        分层存储：worker 只写这里，任务间目录隔离（消除同名互相覆盖）。重跑（重试/打回/retask）
        前，上一轮产物先被整体归档进其下 .history/，当前目录始终只留最新一轮——历史全留、无覆盖。"""
        return self.job_artifacts_dir(goal_id) / task_id

    def agent_dir(self, name: str) -> Path:
        return self.agents_dir / name

    def inbox(self, name: str) -> Path:
        return self.agent_dir(name) / "inbox"

    def outbox(self, name: str) -> Path:
        return self.agent_dir(name) / "outbox"


def load_team(paths: Paths) -> dict[str, Any]:
    with open(paths.team_file, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# 默认 team workspace 标签：team.yaml 未显式指定 herdr.workspace 时的回退名。
DEFAULT_WORKSPACE_LABEL = "TEAMS"


def team_workspace_label(paths: Paths) -> str:
    """team.yaml 里配置的 herdr workspace 标签（未配置回退 DEFAULT_WORKSPACE_LABEL）。

    此前三处（dispatch_task/_ensure_team_wid/reap_team_workspace）各自 inline 同一段
    `(team.get("herdr") or {}).get("workspace", ...)`，易漂移。收敛到单一访问器。
    """
    team = load_team(paths)
    return (team.get("herdr") or {}).get("workspace", DEFAULT_WORKSPACE_LABEL)


def worker_settings_overrides(paths: Paths,
                              manifest: AgentManifest | None = None) -> dict[str, Any]:
    """要写进每个 worker 独占 cwd 的 .claude/settings.json 内容——把协调者专用 skill 对 worker 关掉。

    背景：worker cwd 在深层子目录，Claude Code 从 cwd 向上查到仓库根发现项目 skill，故 worker 会
    继承项目根 .claude/skills/ 里那些只给协调者用的 skill（team-build/team-run/agent-create），
    既占上下文又易让 worker 误以为自己是协调者。目录作用域只能向下、无法藏住根 skill。

    实现选型：用 worker cwd 内的 settings.json（作为常规 settings 层，与用户级鉴权正常合并），
    **不用 `--settings` flag**——实测 claude 传 `--settings <json>`（哪怕 `{}`）会丢失鉴权（Not logged in）。
    settings.json 只从会话 primary working dir 读、不向上查找，故落在 worker cwd 只影响该 worker，
    协调者会话（cwd=仓库根）不受影响；随 cwd 回收即清。

    两级配置，**取并集**（顺序稳定：团队默认在前，agent 追加在后）：
    - 团队级 `defaults.worker_hidden_skills`（skill 名列表）；
    - agent 级 `agents/<name>/agent.yaml` 的 `hidden_skills`（manifest.hidden_skills）。

    并集而非覆盖：团队默认是「所有 worker 都不该看见协调者 skill」的兜底，agent 级只做**追加**。
    若让 agent 级覆盖，任何一个 agent 写错/漏写就会静默把协调者 skill 放回该 worker 上下文，
    而失败方向恰恰是这套隔离最不该出错的方向。要放开某个 skill，就改团队默认那一条。

    两级都空则返回 {}（不写 settings.json）。
    """
    team = load_team(paths)
    hidden = list((team.get("defaults") or {}).get("worker_hidden_skills") or [])
    for name in (manifest.hidden_skills if manifest is not None else []):
        if name not in hidden:
            hidden.append(name)
    if not hidden:
        return {}
    return {"skillOverrides": {name: "off" for name in hidden}}



def load_manifest(paths: Paths, name: str) -> AgentManifest:
    adir = paths.agent_dir(name)
    with open(adir / "agent.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    team = load_team(paths)
    base_rt = (team.get("defaults") or {}).get("runtime", {})
    rt = Runtime.merge(base_rt, raw.get("runtime", {}))
    ws = raw.get("workspace", {}) or {}
    return AgentManifest(
        name=raw.get("name", name),
        role=raw.get("role", ""),
        description=raw.get("description", ""),
        prompt=raw.get("prompt", "prompt.md"),
        runtime=rt,
        workspace_path=ws.get("path", "./workspace"),
        worktree_base=ws.get("worktree_base", ""),
        capabilities=_as_list(raw.get("capabilities")),
        skills=_as_list(raw.get("skills")),
        hidden_skills=_as_list(raw.get("hidden_skills")),
        lifecycle=raw.get("lifecycle",
                          (team.get("defaults") or {}).get("lifecycle", "on-demand")),
        max_concurrent=int(raw.get("max_concurrent", 1)),
        dir=str(adir),
    )


def list_agent_names(paths: Paths) -> list[str]:
    if not paths.agents_dir.exists():
        return []
    return sorted(
        d.name for d in paths.agents_dir.iterdir()
        if d.is_dir() and (d / "agent.yaml").exists()
    )


def resolve_workspace(m: AgentManifest) -> str:
    """把 workspace.path 解析成绝对路径。"""
    p = Path(m.workspace_path)
    if not p.is_absolute():
        p = Path(m.dir) / p
    return os.path.abspath(str(p))


def resolve_task_workspace(m: AgentManifest, goal_id: str, task_id: str) -> str:
    """worker pane 的独占 cwd：<agent workspace>/<goal_id>__<task_id>/。

    每次分派给 worker 一个按任务隔离的子目录，避免同一 agent 被并发拉起（跨 Job / 同 Job
    max_concurrent>1 / 重试）时共用同一 cwd 互相踩踏文件。命名与信号文件
    <goal_id>__<task_id>.json、runtime 复合键 <goal_id>:<task_id> 对齐，便于排查。
    子目录嵌套在基座 workspace 下：worker 靠向上查找继承基座的 CLAUDE.md/.claude，
    基座作只读参考，写入只落本子目录。goal_id/task_id 已是文件名组件（现有约定即安全）。
    """
    base = resolve_workspace(m)
    return os.path.join(base, f"{goal_id}__{task_id}")
