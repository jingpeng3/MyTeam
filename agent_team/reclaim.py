"""回收在途任务的运行期资源（pane / 独占 workspace / 信号 / 账本条目），以及账本对账与 team workspace 回收。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from . import herdr, signals, ledger
from .board import Board
from .config import Paths, load_manifest, resolve_task_workspace, team_workspace_label
from .models import DRY_PANE, runtime_key, split_runtime_key

log = logging.getLogger(__name__)


def session_present(hname: str) -> bool:
    """会话是否仍然「在」——用于**回收保护**，区别于「是否正在推进」（herdr 报 working/busy 等存活态）。

    reconcile 的存活保护此前只认存活态，把 herdr `idle/done`（turn 刚结束、正等 outbox 收割或
    turn 间歇）的**真实存活 worker** 判成「非存活」而回收其 pane。这里把「非 dead、herdr 仍认得这个
    会话」都视为「在」，宁可漏收一个孤儿也不误关一个还活着的 worker（查询失败/gone 才判不在）。"""
    if not hname:
        return False
    try:
        return herdr.agent_status(hname) not in herdr.DEAD_STATES
    except herdr.HerdrError:
        return False


def reconcile_ledger(paths: Paths, scope_goal_id: str | None = None) -> int:
    """账本对账：剔除 runtime.json 里「不对应任何活跃 board dispatched 任务」的孤儿条目。

    修复非对称容错——旧实现只处理「有 dispatched 任务、无账本条目」(ledger-lost)，却从不回收
    反向孤儿「有账本条目、无对应 dispatched 任务」。反向孤儿会**永久占用全局 agent 容量**：
    在稀缺 agent(max_concurrent=1) 拓扑下，一条孤儿即可让整个 Job 所有 ready 任务永远派不出去，
    而 is_stuck 因「ready 可路由」判非卡死 → run 静默空转到 max_ticks。计容量前先对账即可消除。

    **作用域隔离（多会话稳定性）**：给定 `scope_goal_id` 时只对该 Job 自己的 runtime 条目对账，
    绝不枚举/回收别的 Job（可能属别的会话）的条目或 pane——单 Job `tick` 因此只影响自己。
    **存活安全网**：任何条目只要其 pane 仍被 herdr 报为存活（有真在跑的 worker，可能是别的 Job、
    或本 Job board 暂时不同步），一律跳过回收，绝不误杀在跑的 worker。
    """
    rt = ledger.load(paths)
    if not rt:
        return 0
    original = ledger.snapshot(rt)              # 收尾按锁内合并提交，避免与并发驱动者互相覆盖丢更新
    if scope_goal_id is not None:
        candidates = {k: v for k, v in rt.items() if split_runtime_key(k)[0] == scope_goal_id}
        b = Board.load(paths, scope_goal_id)
        boards = [b] if b is not None else []
    else:
        candidates = dict(rt)
        boards = Board.load_all(paths)
    live: set[str] = set()
    for b in boards:
        # 在途 = dispatched + awaiting_input：打回升级 parked 的条目 pane 已关、herdr 报 gone，
        # 只认 dispatched 会把它当孤儿回收 → monitor 判 ledger-lost 重派，绕过人工裁决闸门
        for t in b.pollable_tasks():
            live.add(runtime_key(b.goal_id, t.task_id))
    orphans: dict[str, Any] = {}
    for k, v in candidates.items():
        if k in live or v.get("pane_id") == DRY_PANE:
            continue
        if session_present(v.get("herdr_name", "")):   # 会话仍在(含 idle/done)→保护，不误关活 worker
            continue
        orphans[k] = v
    if not orphans:
        return 0
    kept = {k: v for k, v in rt.items() if k not in orphans}
    for k, v in orphans.items():
        close_pane_safe(paths, v.get("pane_id", ""))   # 顺手回收其可能残留的 pane
        gid_k, tid_k = split_runtime_key(k)
        if tid_k:                                       # 连带清其残留 scratch 子目录
            clear_task_workspace(paths, v.get("agent", ""), gid_k, tid_k)
        ledger.log_event(paths, "ledger_orphan_reaped", key=k, agent=v.get("agent"))
    ledger.commit_merge(paths, kept, original)
    return len(orphans)


def close_pane_safe(paths: Paths, pane_id: str) -> None:
    if not pane_id or pane_id == DRY_PANE:
        return
    # 绝不回收协调者自己所在的 pane：若 provisioning 因故把 root/self pane 误记为 worker
    # pane，关它就等于杀掉当前 herdr 会话。宁可漏收一个孤儿，也不能丢会话。
    if pane_id == herdr.current_pane_id():
        ledger.log_event(paths, "close_pane_skipped_self", pane_id=pane_id)
        return
    try:
        herdr.close_pane(pane_id)
        ledger.log_event(paths, "pane_closed", pane_id=pane_id)   # 正向遥测：形成 pane 生命周期审计链
    except herdr.HerdrError as e:
        ledger.log_event(paths, "close_pane_failed", pane_id=pane_id, error=str(e))


def clear_task_workspace(paths: Paths, agent_name: str, goal_id: str,
                          task_id: str) -> None:
    """回收 worker 的按任务独占 cwd（<agent workspace>/<goal_id>__<task_id>/）。

    scratch 目录生命周期 = pane 生命周期：任务终态/重试/绕过/删 Job 时随 pane 回收一并清掉，
    避免残留污染同 agent 的后续任务（含同任务的下次重试从脏目录起跑）。产物是交付物、落在独立的
    绝对 artifacts_dir，不在此目录，清掉不丢交付。best-effort：拿不到 manifest 或目录不存在即跳过。
    """
    if not agent_name:
        return
    try:
        m = load_manifest(paths, agent_name)
    except Exception as e:                  # noqa: BLE001  manifest 已删/损坏等：无从定位目录，跳过
        log.debug("清理任务 workspace 跳过：无法加载 agent %s 的 manifest：%s", agent_name, e)
        return
    d = Path(resolve_task_workspace(m, goal_id, task_id))
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        ledger.log_event(paths, "task_workspace_cleared",
                   goal_id=goal_id, task_id=task_id, agent=agent_name)


def reclaim_task(paths: Paths, rt: dict[str, Any], gid: str, tid: str, fallback_agent: str, *,
             close_pane: bool = True, quarantine: bool = False, pop: bool = True) -> dict[str, Any]:
    """回收一个在途任务的运行期资源：剔 runtime 条目 → 关 pane + 清独占 workspace → 清 in/outbox 信号。

    monitor 收尾、skip/retask/reject 等所有「结束一次在途尝试」的路径共用这一处，保证四样东西一起收干净。
    - 只改传入的 rt（内存），由调用方统一提交（ledger.commit_merge），与长流程共享同一份账本。
    - agent 以账本记录为准、缺失/空串再回退 fallback_agent（调用方可能已清掉 task.agent，需先存好）。
    - close_pane=False：dry-run 不碰 pane/workspace，只清账本与信号。
    - quarantine：outbox 判 malformed 时隔离取证而非删除。
    - pop=False：保留账本条目（打回升级 parked 的打回者还要在条目上记 escalated）。
    返回被回收的账本条目（无则 {}）。"""
    key = runtime_key(gid, tid)
    info = rt.pop(key, {}) if pop else rt.get(key, {})
    agent = info.get("agent") or fallback_agent
    if close_pane:
        close_pane_safe(paths, info.get("pane_id", ""))
        clear_task_workspace(paths, agent, gid, tid)
    if agent:
        signals.clear_task_signals(paths, agent, gid, tid, quarantine_outbox=quarantine)
    return info


def close_goal_panes(paths: Paths, goal_id: str) -> int:
    """回收某 Job 在途(dispatched)的所有 pane，并把其 runtime 条目剔除。

    修复「重载 plan / 删除 Job 只清信号与账本、却遗弃在途 pane」导致的孤儿 pane 泄漏。
    """
    rt = ledger.load(paths)
    original = ledger.snapshot(rt)
    closed = 0
    remaining = {}
    for k, v in rt.items():
        kgid, ktid = split_runtime_key(k)
        if v.get("goal_id") == goal_id or kgid == goal_id:
            pid = v.get("pane_id", "")
            if pid and pid != DRY_PANE:
                close_pane_safe(paths, pid)
                closed += 1
            # 随 pane 一并清该任务的 scratch 子目录（删 Job / 重载同名 plan 时的残留）
            tid = ktid or v.get("task_id", "")
            if tid:
                clear_task_workspace(paths, v.get("agent", ""), goal_id, tid)
        else:
            remaining[k] = v
    if remaining != rt:
        ledger.commit_merge(paths, remaining, original)
    if closed:
        ledger.log_event(paths, "goal_panes_closed", goal_id=goal_id, count=closed)
    return closed


def reap_team_workspace(paths: Paths) -> bool:
    """若框架自建的 team workspace 已无活跃 pane，则连同其空闲 root pane 一并关闭。

    ensure_workspace 每次为 team 建的 workspace + root pane 在 Job 生命周期结束后无人回收，
    多轮运行会累积孤儿。仅在无在途 pane 时回收，保守且只动框架自己创建的 workspace。

    关键护栏：team.yaml 的 workspace label 常与协调者当前会话所在的 workspace 同名
    （本项目默认 "TEAMS"），此时 ensure_workspace 复用的是协调者自己所在的 workspace，
    它的 root pane 就是当前 herdr 会话。close 它 = 直接丢会话。只要团队 workspace 解析到
    当前会话 workspace，就绝不回收——这不是框架新建的空壳，而是活着的宿主会话。
    """
    if ledger.load(paths):              # 还有在途 pane 记录，说明仍在用，不动
        return False
    label = team_workspace_label(paths)
    try:
        wid = herdr.find_workspace(label)
        if not wid:
            return False
        if wid == herdr.current_workspace_id():   # 就是协调者宿主会话 → 绝不 close
            ledger.log_event(paths, "workspace_reap_skipped_self",
                       workspace=label, workspace_id=wid)
            return False
        if herdr.workspace_pane_count(wid) > 1:   # 除 root pane 外仍有 pane，别误关
            return False
        herdr.close_workspace(wid)
        ledger.log_event(paths, "workspace_reaped", workspace=label, workspace_id=wid)
        return True
    except herdr.HerdrError as e:
        ledger.log_event(paths, "workspace_reap_failed", workspace=label, error=str(e))
        return False
