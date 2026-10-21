"""分派：容量感知选人 → 并发 herdr 拉起 → 主线程串行提交账本/board。"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import herdr, signals, registry, ledger, bootstrap
from .board import Board
from .config import (Paths, load_manifest, resolve_workspace, resolve_task_workspace,
                     worker_settings_overrides, atomic_write_json, team_workspace_label)
from .models import Task, AgentManifest, DISPATCHED, FAILED, DRY_PANE, runtime_key
from .registry import eligible_agents, best_agent

log = logging.getLogger(__name__)


# provisioning（herdr 拉起 pane/agent）失败的独立重试上限：与业务 max_retries 隔离，
# 一次 herdr 抖动不应消耗任务的业务重试预算、更不应误灭整棵下游子树。
PROVISION_MAX_RETRIES = 2
# 单轮 tick 内并发拉起的 pane 上限：herdr create_pane/start_agent/prompt 是各自独立的阻塞
# subprocess 往返，串行拉起会把「一轮分派」拉成 N×~12s、并在此期间冻结所有在途 worker 的
# outbox 收割与超时判定。并发 provisioning 把这段关键路径压回 ~1 个 pane 启动时长。
PROVISION_PARALLELISM = 6


class ProvisionError(Exception):
    """herdr 拉起 worker（create_pane/start_agent/prompt）失败——基础设施问题，非任务本身坏。

    与业务失败（worker 报 status=failed）语义不同：走独立的 infra 重试预算，不计入
    max_retries、不污染 tried_agents，避免「连 worker 都没起起来」被当成一次业务尝试。
    """


def eligible_candidates(paths: Paths, task: Task,
                         agents: list[AgentManifest] | None = None) -> list[AgentManifest]:
    """按能力匹配数从高到低的候选；有能力要求时滤掉零匹配者。select_agent 与
    all_eligible_tried 共用同一条候选口径，避免两处过滤逻辑漂移。"""
    cands = eligible_agents(paths, task.capabilities, agents=agents)
    if task.capabilities:
        cands = [m for m in cands if m.matches(task.capabilities) > 0]
    return cands


def select_agent(paths: Paths, task: Task, active: dict[tuple[str, str], int],
                 reserved: dict[tuple[str, str], int], goal_id: str,
                 agents: list[AgentManifest] | None = None) -> AgentManifest | None:
    """容量感知地为任务挑 agent：能力匹配从高到低，跳过在**本 Job 内**已达 max_concurrent 的。

    active = 本轮开始前各 (Job, agent) 已占用数；reserved = 本轮已预占数。两者相加即该 Job 当前
    占用，避免同一 agent 在本 Job 内被挤爆或重复计数。容量按 (goal_id, agent) 独立结算——
    **不跨 Job 共享**：别的 Job 占满某 agent 不影响本 Job 仍可跑满自己的 max_concurrent。
    `agents` 为本轮预加载的 manifest 列表（避免逐节点重读 agent.yaml）。
    """
    if task.agent:                       # 已指定(如预分派)则沿用
        return load_manifest(paths, task.agent)
    cands = eligible_candidates(paths, task, agents=agents)

    def least_loaded_free(lst: list[AgentManifest]) -> AgentManifest | None:
        # 挑「剩余空闲容量最多」的候选（负载均衡），并列时保持 eligible 稳定序——
        # 避免 first-fit 把串行/瓶颈层恒定压向字母序最靠前的同能力 agent。
        best, best_free = None, 0
        for m in lst:
            used = active.get((goal_id, m.name), 0) + reserved.get((goal_id, m.name), 0)
            free = m.max_concurrent - used
            if free > best_free:
                best, best_free = m, free
        return best

    # 重试优先换到「没试过」的 eligible agent，绕开对该任务确定性失败的坏节点。
    # 关键修正：只要还存在未试 agent，就只在未试里挑——它们即便当下容量占满也应**等容量**
    # （返回 None → 本轮 waiting_for_agent、下轮再换），绝不回退去重用已知会失败的 tried agent
    # （否则一次本可在别人身上成功的失败会被白烧在坏 agent 上、甚至在从未真正试过备胎时判死）。
    untried = [m for m in cands if m.name not in task.tried_agents]
    if untried:
        return least_loaded_free(untried)
    return least_loaded_free(cands)      # 真的全试过了，才回退全体（含原 agent）


def all_eligible_tried(paths: Paths, task: Task,
                        agents: list[AgentManifest] | None = None) -> bool:
    """本任务的全部 eligible agent 是否都已在 tried_agents 里（含只有单一 eligible 的情形）。
    为真=「换人已无意义」，是标注 no_alternate_agent 的真正条件。"""
    cands = eligible_candidates(paths, task, agents=agents)
    if not cands:
        return True
    return all(m.name in task.tried_agents for m in cands)


def archive_task_artifacts(paths: Paths, gid: str, task: Task) -> None:
    """（重）派发前把任务「当前」产物目录里的上一轮产物整体归档进 .history/<stamp>/，留空当前目录。

    分层存储 + 保留历史：每个任务产物独占 shared/artifacts/<gid>/<tid>/；重试/打回/retask 重派时，
    旧产物先移进 .history（stamp 带 attempts/reject_count 便于溯源），当前目录清空给新一轮 worker 写——
    历史全留（永不删）、当前目录只含最新一轮，杜绝同名覆盖丢历史与旧产物混入。首派目录空则无操作。

    线程安全：只碰本任务独占的产物目录（<gid>/<tid>/），不触共享 runtime/board，可在并发 provisioning
    里调用。dashboard 不受影响：面板只展示 worker 上报的 t.artifacts（指向当前目录），不扫目录、不列 .history。
    """
    tdir = paths.task_artifacts_dir(gid, task.task_id)
    tdir.mkdir(parents=True, exist_ok=True)              # 顺带建父级 <gid>/（替代旧的 job dir mkdir）
    # .history（历史归档）与 .rejections（打回证据存根）是框架管理目录、非本轮交付，永不归档移动。
    reserved = {".history", ".rejections"}
    live = [p for p in tdir.iterdir() if p.name not in reserved]
    if not live:                                         # 首派或上一轮无产物：无需归档
        return
    hist = tdir / ".history"
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-a{task.attempts}-r{task.reject_count}"
    dest = hist / stamp
    if dest.exists():                                    # 同秒重派兜底：加毫秒后缀防撞
        dest = hist / f"{stamp}-{int(time.time() * 1000) % 1000:03d}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in live:
        shutil.move(str(p), str(dest / p.name))
    ledger.log_event(paths, "task_artifacts_archived", goal_id=gid, task_id=task.task_id,
               archived_to=str(dest), files=len(live))


def _write_worker_skill_overrides(paths: Paths, cwd: str, m: AgentManifest) -> None:
    """在 worker 独占 cwd 写 .claude/settings.json，把协调者专用 skill 对该 worker 关掉。

    见 config.worker_settings_overrides：用 cwd 内 settings.json 而非 `--settings` flag（后者会让
    ducc 丢鉴权）；也不放在 agents/<name>/ 下——实测 settings.json **不向上查找**，祖先目录那份不会
    被读到，只有 worker 的 primary working dir 里的才生效，故必须每次分派现写。隐藏列表 =
    团队默认 ∪ 该 agent 的 hidden_skills；两级都空时返回空、跳过不写。每次（重）派发都重写，
    idempotent；cwd 随 pane 回收即清，无残留。写失败不阻断分派（skill 裁剪是优化非关键路径）。
    """
    ov = worker_settings_overrides(paths, m)
    if not ov:
        return
    try:
        sdir = Path(cwd) / ".claude"
        sdir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(sdir / "settings.json", ov)
    except OSError as e:
        log.debug("写 worker skill overrides 失败(%s)，忽略：%s", cwd, e)


def _provision_herdr(paths: Paths, wid: str, m: AgentManifest, gid: str,
                     task: Task, done_deps: list[str],
                     inherited: list[dict] | None = None) -> tuple[str, str]:
    """纯 herdr 拉起 + 写本任务 inbox；**线程安全**（只碰 herdr 与本任务独占的 inbox 文件，
    不读写共享的 runtime.json/board）。返回 (pane_id, herdr_name)，失败抛 ProvisionError。

    inbox 写入推迟到 start_agent 成功之后、prompt 之前——避免「早写 inbox 但 provisioning 失败/
    换人」后遗留无人回收的孤儿 inbox；失败路径显式清掉可能已写的 inbox。
    """
    cwd = resolve_task_workspace(m, gid, task.task_id)   # 按任务独占子目录（隔离并发/重试）
    os.makedirs(cwd, exist_ok=True)                       # 深路径自动建父级基座 workspace
    _write_worker_skill_overrides(paths, cwd, m)         # cwd 内落 settings.json：对本 worker 关掉协调者专用 skill + 该 agent 追加的 hidden_skills
    archive_task_artifacts(paths, gid, task)            # 重派前归档上一轮产物、留空当前目录（首派无操作，顺带建产物目录）
    # herdr agent 名须唯一；a{业务尝试}p{infra尝试} 让每次(重)分派、每次 infra 重试都用新名。
    label = f"{m.name}:{gid}:{task.task_id}:a{task.attempts + 1}p{task.provision_attempts}"
    try:
        pane_id = herdr.create_pane(wid, cwd, label)
    except herdr.HerdrError as e:
        raise ProvisionError(f"create_pane failed: {e}") from e
    try:
        agent_name = herdr.start_agent(pane_id, label, m.runtime.kind,
                                       m.runtime.flags or None,
                                       model=m.runtime.model)
        bootstrap.write_inbox_signal(paths, m.name, gid, task, done_deps, inherited)
        # 分派 fire-and-forget：完成靠 outbox 信号，不在此阻塞等 turn 结束
        herdr.prompt_agent(agent_name, bootstrap.build_bootstrap(paths, m, gid, task))
    except herdr.HerdrError as e:
        with contextlib.suppress(herdr.HerdrError):
            herdr.close_pane(pane_id)
        signals.clear_task_signals(paths, m.name, gid, task.task_id)   # 清掉可能已写的 inbox
        ledger.log_event(paths, "dispatch_provision_failed", goal_id=gid,
                   task_id=task.task_id, agent=m.name, pane_id=pane_id,
                   provision_attempt=task.provision_attempts)
        raise ProvisionError(f"start/prompt failed: {e}") from e
    return pane_id, agent_name


def _timed_provision(*args: Any) -> tuple[str, str, float]:
    """_provision_herdr + 计时：返回 (pane_id, herdr_name, 拉起耗时秒)，供运行记录的 provision_s。"""
    t0 = time.time()
    pane_id, agent_name = _provision_herdr(*args)
    return pane_id, agent_name, time.time() - t0


def _commit_dispatch(paths: Paths, board: Board, task: Task, m: AgentManifest,
                     pane_id: str, agent_name: str,
                     provision_s: float | None = None) -> None:
    """provisioning 成功后在**主线程**串行提交状态：bump 业务尝试、写账本、落盘、记事件。"""
    gid = board.goal_id
    rkey = runtime_key(gid, task.task_id)
    task.agent = m.name                  # 关键：monitor 按 task.agent 读 outbox，必须回填被分派的 agent
    task.attempts += 1                   # worker 已接单，才算一次真正的业务尝试
    task.provision_attempts = 0
    task.artifact_warnings = []          # 上一轮交付的产物哨兵已随重派作废，别把旧告警挂在新的尝试上
    if m.name not in task.tried_agents:  # 记录试过谁，供下次重试换人绕开
        task.tried_agents.append(m.name)
    now = time.time()
    with ledger.lock(paths):                    # 原子读改写：本进程的新分派条目不被并发驱动者覆盖
        rt = ledger.load(paths)
        rt[rkey] = {"goal_id": gid, "task_id": task.task_id, "agent": m.name,
                    "pane_id": pane_id, "herdr_name": agent_name,
                    "session": herdr.current_session(),  # worker 所在 herdr session，供跨进程 dashboard 定位
                    "dispatched_at": now,        # 首次分派时刻，绝对上限用，永不刷新
                    "last_alive_at": now,        # 存活心跳，静默死亡判定用，可刷新
                    "idle_since": None}          # 首次观测到 idle 的时刻，宽限窗锚点
        ledger.save(paths, rt)
    task.status = DISPATCHED
    task.open_run(m.name, now, provision_s)        # 计时起点：worker 已接单
    board.save(paths)
    ledger.log_event(paths, "dispatched", goal_id=gid, task_id=task.task_id,
               agent=m.name, attempt=task.attempts, dry_run=False)


def _handle_provision_fail(paths: Paths, board: Board, task: Task,
                           err: Exception) -> None:
    """provisioning 耗尽/失败的收尾：走独立 infra 预算，不消耗 max_retries、不污染 tried_agents。"""
    task.provision_attempts += 1
    if task.provision_attempts > PROVISION_MAX_RETRIES:
        task.status = FAILED
        task.error = f"provisioning failed after {task.provision_attempts} tries: {err}"
        ledger.log_event(paths, "dispatch_error", goal_id=board.goal_id,
                   task_id=task.task_id, error=str(err), kind="provision_exhausted",
                   new_status=task.status)
    else:
        task.agent = ""              # infra 重试下轮可容量感知地换 eligible agent（不算换人绕障）
        ledger.log_event(paths, "provision_retry", goal_id=board.goal_id,
                   task_id=task.task_id, provision_attempt=task.provision_attempts)
    board.save(paths)


def dispatch_task(paths: Paths, board: Board, task: Task,
                  manifest: AgentManifest | None = None,
                  dry_run: bool = False) -> str:
    """为一个 ready 任务分派 agent（串行路径，供 dry-run 与直接调用）；返回被分派的 agent name。"""
    m = manifest or (load_manifest(paths, task.agent) if task.agent
                     else best_agent(paths, task.capabilities))
    if m is None:
        raise RuntimeError(f"没有可匹配 {task.capabilities} 的 agent")
    task.agent = m.name
    gid = board.goal_id
    rkey = runtime_key(gid, task.task_id)

    if dry_run:                          # 纯预览：只写信号、不拉 pane、不 +attempts/不改 tried_agents
        bootstrap.write_inbox_signal(paths, m.name, gid, task, board.done_deps(task),
                               board.inherited_artifacts(task))
        now = time.time()
        with ledger.lock(paths):
            rt = ledger.load(paths)
            rt[rkey] = {"goal_id": gid, "task_id": task.task_id, "agent": m.name,
                        "pane_id": DRY_PANE, "dispatched_at": now}
            ledger.save(paths, rt)
        task.status = DISPATCHED
        task.open_run(m.name, now)       # dry-run 也开记录，让 report/看板的计时链路可预览
        board.save(paths)
        ledger.log_event(paths, "dispatched", goal_id=gid, task_id=task.task_id,
                   agent=m.name, attempt=task.attempts, dry_run=True)
        return m.name

    wid = _ensure_team_wid(paths, resolve_workspace(m))
    pane_id, agent_name, prov_s = _timed_provision(paths, wid, m, gid, task,
                                                   board.done_deps(task),
                                                   board.inherited_artifacts(task))
    _commit_dispatch(paths, board, task, m, pane_id, agent_name, provision_s=prov_s)
    return m.name


def _ensure_team_wid(paths: Paths, fallback_cwd: str) -> str:
    return herdr.ensure_workspace(team_workspace_label(paths), fallback_cwd)


def _active_by_job_group(boards: list[Board]) -> dict[tuple[str, str], int]:
    """当前每个 (Job, fanout_group) 占用的并发数——fan-out 子任务的**组级并发闸**结算依据。

    与 agent 容量（ledger.active_by_job_agent 按 runtime.json 结算）不同，组并发按 **board 状态**结算：
    统计各 Job 里 fanout_group 非空、且仍持有 pane（DISPATCHED/AWAITING_INPUT）的子任务数。
    组闸给了「fan-out 节点独立于 agent.max_concurrent 的自己的同时并发上限」——同组超上限的 ready
    子任务留在 READY、下轮等同组先完成者释放后再派（与 agent 容量的「等容量」语义同构）。
    """
    return {(b.goal_id, g): n for b in boards for g, n in b.active_by_group().items()}


def run_dispatch_round(paths: Paths, boards: list[Board],
                        dry_run: bool) -> dict[str, dict[str, list[str]]]:
    """一轮跨给定 boards 的分派：选人(容量感知+公平) → **并发** provisioning → 主线程串行提交。

    返回 {goal_id: {"dispatched":[...], "waiting_for_agent":[...], "waiting_for_capacity":[...],
    "dispatch_errors":[...]}}。
    并发化 provisioning 是 round-4 头号共识修复：herdr 拉起是各自独立的阻塞 subprocess，串行会把
    一轮分派拉成 N×~12s 并冻结所有在途 worker 的监控/超时判定；这里把拉起并发、状态提交仍串行。
    """
    active = ledger.active_by_job_agent(paths)
    reserved: dict[tuple[str, str], int] = {}
    group_active = _active_by_job_group(boards)          # fan-out 组级并发闸：本轮开始前各组在途数
    group_reserved: dict[tuple[str, str], int] = {}
    agents_cache = registry.list_agents(paths)     # 本轮只加载一次全部 manifest（避免逐节点重读）
    out = {b.goal_id: {"dispatched": [], "waiting_for_agent": [],
                       "waiting_for_capacity": [], "dispatch_errors": []}
           for b in boards}
    selection: list[tuple[Board, Task, AgentManifest]] = []
    # 跨 Job 公平：在途任务最少（最饿）的 Job 先挑稀缺容量，避免字典序靠后的 Job 被持续压后。
    for b in sorted(boards, key=lambda b: (len(b.dispatched_tasks()), b.goal_id)):
        for t in b.ready_tasks():
            # fan-out 组级并发闸：同组在途+本轮预占已达上限 → 留 READY 等容量（不占 agent 选择）。
            gkey = (b.goal_id, t.fanout_group) if t.fanout_group else None
            if gkey and t.fanout_max_parallel:
                used = group_active.get(gkey, 0) + group_reserved.get(gkey, 0)
                if used >= t.fanout_max_parallel:
                    out[b.goal_id]["waiting_for_capacity"].append(t.task_id)
                    continue
            m = select_agent(paths, t, active, reserved, b.goal_id, agents=agents_cache)
            if m is None:
                out[b.goal_id]["waiting_for_agent"].append(t.task_id)
                continue
            reserved[(b.goal_id, m.name)] = reserved.get((b.goal_id, m.name), 0) + 1
            if gkey and t.fanout_max_parallel:
                group_reserved[gkey] = group_reserved.get(gkey, 0) + 1
            selection.append((b, t, m))

    if not selection:
        return out

    if dry_run:
        for b, t, m in selection:
            if _try_dispatch(paths, b, t, m, dry_run=True):
                out[b.goal_id]["dispatched"].append(t.task_id)
            else:
                out[b.goal_id]["dispatch_errors"].append(t.task_id)
        return out

    # 真实分派：先在主线程算好各任务的 done_deps 与继承产物（board 只读），再并发拉起 pane。
    wid = _ensure_team_wid(paths, resolve_workspace(selection[0][2]))
    jobs = [(b, t, m, b.done_deps(t), b.inherited_artifacts(t)) for b, t, m in selection]
    provisioned: dict[int, Any] = {}
    workers = min(len(jobs), PROVISION_PARALLELISM)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {i: ex.submit(_timed_provision, paths, wid, m, b.goal_id, t, dd, inh)
                for i, (b, t, m, dd, inh) in enumerate(jobs)}
        for i, fut in futs.items():
            try:
                provisioned[i] = fut.result()
            except Exception as e:          # noqa: BLE001 —— 每任务隔离，异常留待主线程按类处置
                provisioned[i] = e
    # 主线程按 selection 顺序串行提交状态（写账本/落盘/记事件），保证一致性。
    for i, (b, t, m, _dd, _inh) in enumerate(jobs):
        res = provisioned[i]
        if isinstance(res, tuple):
            _commit_dispatch(paths, b, t, m, res[0], res[1], provision_s=res[2])
            out[b.goal_id]["dispatched"].append(t.task_id)
        elif isinstance(res, ProvisionError):
            _handle_provision_fail(paths, b, t, res)
            out[b.goal_id]["dispatch_errors"].append(t.task_id)
        else:
            # provisioning 线程抛出的非 ProvisionError 异常（如 resolve_workspace/os.makedirs 失败、
            # future 里的意外异常）：worker 从未接单，与 ProvisionError 同为「分派期失败」。走独立
            # infra 预算(_handle_provision_fail)终态化——不能裸调 monitor._mark_retry_or_failed：那以 attempts
            # 为判据，而 attempts 仅在 _commit_dispatch 自增，此路径没跑到，会永不达上限、每轮重派不止。
            _handle_provision_fail(paths, b, t, res)
            ledger.log_event(paths, "dispatch_error", goal_id=b.goal_id,
                       task_id=t.task_id, error=str(res), new_status=t.status)
            out[b.goal_id]["dispatch_errors"].append(t.task_id)
    return out


def _try_dispatch(paths: Paths, board: Board, task: Task,
                  manifest: AgentManifest, dry_run: bool) -> bool:
    """隔离单任务分派异常：失败不外抛，标记重试/失败并记录，让本轮其它节点继续。

    区分两类失败：
    - ProvisionError（herdr 拉起失败）：基础设施抖动，走独立 infra 重试预算，**不消耗 max_retries、
      不污染 tried_agents**；仅 infra 重试也耗尽才升级为任务失败并明确标注，避免误灭整棵下游子树。
    - 其它分派期异常（无匹配 agent / OSError / HerdrError 等）：worker 同样从未接单，与 ProvisionError
      同类，一并走 infra 预算终态化。**不能裸调 monitor._mark_retry_or_failed**——它以 attempts 为判据，而
      attempts 仅在成功接单(_commit_dispatch)时自增，分派期失败此值恒为 0，会永不达上限、每轮重派不止
      （尤其 tick-only 逐轮驱动无 idle_streak 兜底时）。
    """
    try:
        dispatch_task(paths, board, task, manifest=manifest, dry_run=dry_run)
        return True
    except ProvisionError as e:
        _handle_provision_fail(paths, board, task, e)   # 独立 infra 预算，与并发路径同一处理
        return False
    except (RuntimeError, OSError) as e:      # 含 herdr.HerdrError（RuntimeError 子类）
        _handle_provision_fail(paths, board, task, e)   # 分派期失败：同走 infra 预算，保证有界终态
        ledger.log_event(paths, "dispatch_error", goal_id=board.goal_id,
                   task_id=task.task_id, error=str(e), new_status=task.status)
        return False


def routable(paths: Paths, task: Task, agents: list[AgentManifest] | None = None) -> bool:
    """是否存在能力匹配的 agent（容量满是暂时的，能力不匹配是永久的）。"""
    if not task.capabilities:
        return True
    return bool(eligible_candidates(paths, task, agents=agents))
