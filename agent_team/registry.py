"""Agent 维护：创建 / 列出 / 查看 / 删除 agent 目录。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import (
    Paths, load_manifest, load_team, list_agent_names,
    atomic_write_yaml, file_lock,
)
from .models import AgentManifest, DEFAULT_CLI, DEFAULT_KIND

log = logging.getLogger(__name__)

# 只写角色自己的工作内容：读 inbox → 写 outbox 的通用交付协议由框架在引导语里统一下发
# （agent_team/worker_protocol.md），不要在 prompt.md 里再抄一份。
_PROMPT_TMPL = """\
# {name}

你是 agent-team 中的 **{role}** worker。职责：<一句话说清本角色做什么、不做什么>。

## 步骤
1. <本角色怎么干活；需要的编译/测试按项目约定跑通>

## 产出
- `artifacts_dir/<文件>`：<产物说明>
- summary 写：<下游/协调者需要从 summary 里看到什么>
"""


def create_agent(paths: Paths, name: str, role: str = "",
                 capabilities: list[str] | None = None,
                 max_concurrent: int = 1) -> Path:
    adir = paths.agent_dir(name)
    if adir.exists():
        raise FileExistsError(f"agent 已存在: {name}")
    (adir / "workspace").mkdir(parents=True)
    (adir / "inbox").mkdir()
    (adir / "outbox").mkdir()
    (adir / "logs").mkdir()
    caps = capabilities or []
    # 用 yaml.safe_dump 构造 manifest，而非文本模板 .format——role/capabilities 里含 `:`、`#`、引号、
    # 换行等 YAML 元字符时，文本拼接会写出**非法 YAML**，而 registry 加载**全部** manifest，一个坏文件
    # 就让 `agent list`/`tick` 对所有 Job 崩栈。safe_dump 会按需加引号/转义，保证始终是合法 YAML。
    manifest = {
        "name": name,
        "role": role,
        "description": "",
        "prompt": "prompt.md",
        "runtime": {"cli": DEFAULT_CLI, "kind": DEFAULT_KIND, "model": "", "flags": []},
        "workspace": {"path": "./workspace", "worktree_base": ""},
        "capabilities": list(caps),
        "skills": [],
        "hidden_skills": [],
        "lifecycle": "on-demand",
        "max_concurrent": int(max_concurrent),
    }
    atomic_write_yaml(adir / "agent.yaml", manifest)
    (adir / "prompt.md").write_text(
        _PROMPT_TMPL.format(name=name, role=role or "通用"), encoding="utf-8",
    )
    _register(paths, name)
    return adir


def _register(paths: Paths, name: str) -> None:
    """把 name 追加进 team.yaml 的 agents 列表（并发安全：全程持锁 + 原子写）。"""
    with file_lock(paths.team_file):
        team = load_team(paths)
        agents = team.get("agents") or []
        if name not in agents:
            agents.append(name)
            team["agents"] = agents
            atomic_write_yaml(paths.team_file, team)


def _unregister(paths: Paths, name: str) -> None:
    with file_lock(paths.team_file):
        team = load_team(paths)
        agents = [a for a in (team.get("agents") or []) if a != name]
        team["agents"] = agents
        atomic_write_yaml(paths.team_file, team)


def remove_agent(paths: Paths, name: str, purge: bool = False) -> bool:
    """摘除 agent。返回是否确实存在并被处理——删不存在的 agent 返回 False（调用方据此非零退出、
    不谎报成功），且不无谓重写 team.yaml。存在性以「目录存在 或 已登记在 team.yaml」为准。"""
    adir = paths.agent_dir(name)
    registered = name in (load_team(paths).get("agents") or [])
    if not adir.exists() and not registered:
        return False
    _unregister(paths, name)
    if purge and adir.exists():
        shutil.rmtree(adir)
    return True


def list_agents(paths: Paths) -> list[AgentManifest]:
    """加载全部 agent manifest。**单个坏 manifest（非法 YAML/损坏）跳过并告警，绝不拖垮全体**——
    registry 是 tick/best_agent/dashboard 的入口，一个坏文件若裸抛会让所有 Job 的调度崩栈。"""
    out: list[AgentManifest] = []
    for n in list_agent_names(paths):
        try:
            out.append(load_manifest(paths, n))
        except Exception as e:                       # noqa: BLE001 —— 坏 manifest 隔离跳过
            log.warning("跳过无法加载的 agent manifest %s：%s", n, e)
    return out


def best_agent(paths: Paths, capabilities: list[str]) -> AgentManifest | None:
    """按能力匹配数打分选最合适的 agent；有能力要求却无人匹配时返回 None。

    与 coordinator.select_agent 语义对齐：绝不把有能力要求的任务硬塞给零匹配 agent，
    避免两条派发路径对「无匹配」给出相反结果造成误路由。
    """
    cands = list_agents(paths)
    if not cands:
        return None
    scored = sorted(cands, key=lambda m: m.matches(capabilities), reverse=True)
    top = scored[0]
    if capabilities and top.matches(capabilities) == 0:
        return None
    return top


def eligible_agents(paths: Paths, capabilities: list[str],
                    agents: list[AgentManifest] | None = None) -> list[AgentManifest]:
    """按能力匹配数从高到低返回候选（供容量感知的分派挑选）。

    `agents` 可传入本轮预加载的 manifest 列表，避免对每个 ready 节点都重读全部 agent.yaml
    （宽扇出下 O(ready×agents) 次 YAML 解析）；不传则退回现读现解析。
    """
    cands = agents if agents is not None else list_agents(paths)
    return sorted(cands, key=lambda m: m.matches(capabilities), reverse=True)

