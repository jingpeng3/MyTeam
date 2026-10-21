"""协调者运行期干预：answer（回答等人）、skip（绕过）、retask（改写 goal）、reject（打回重跑）、expand（fan-out 展开）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import herdr, ledger, dispatch, reclaim
from .board import Board, DagError
from .config import Paths
from .models import (DISPATCHED, SKIPPED, READY, PENDING,
                     BYPASSED, AWAITING_INPUT, UNFILLED_SLOT_RE,
                     TERMINAL, INFLIGHT, runtime_key)


# 单个打回目标节点的**默认累计被打回上限**：task.max_rejects>0 时以任务为准。
# 达到上限即不再自动回退，把打回者升级为 AWAITING_INPUT(escalated) 交协调者裁决，防实现↔评审无限乒乓。
MAX_REJECTS = 2


def _stash_reject_evidence(paths: Paths, gid: str, target_id: str,
                           reject_count: int, evidence: list[str] | None) -> list[str]:
    """把打回证据文件复制到目标节点的 .rejections/r<N>/ 存根区，返回稳定路径（供凭证长期引用）。

    为何要复制：证据通常写在**打回者自己**的产物目录里，而打回者随后会连同后代一起重跑——其目录一旦
    被 dispatch.archive_task_artifacts 归档，原证据路径即失效。凭证承诺「历次意见+证据永不清空」，故把证据快照进
    目标节点名下的 .rejections/（该目录被归档逻辑豁免、永不移动），令历史证据在多次打回后仍可读。
    best-effort：源文件缺失/复制失败时回退原路径（不因证据存根失败阻断打回主流程）。
    """
    if not evidence:
        return []
    stash = paths.task_artifacts_dir(gid, target_id) / ".rejections" / f"r{reject_count}"
    out: list[str] = []
    for src in evidence:
        try:
            sp = Path(src)
            if sp.is_file():
                stash.mkdir(parents=True, exist_ok=True)
                dst = stash / sp.name
                shutil.copy2(str(sp), str(dst))
                out.append(str(dst))
            else:
                out.append(src)                 # 非文件（目录/不存在）：原样保留引用
        except OSError:
            out.append(src)                     # 复制失败：回退原路径，不阻断打回
    return out


def _with_progress(out: dict[str, Any], board: Board) -> dict[str, Any]:
    """协调者干预动作（skip/retask/reject/expand）的返回值统一附上 Job 进度。"""
    out["complete"] = board.is_complete()
    out["summary"] = board.summary()
    return out


def answer_task(paths: Paths, board: Board, task_id: str, text: str) -> dict[str, Any]:
    """把人的答复注入某个 AWAITING_INPUT 任务的 worker pane，让它继续执行。

    经 runtime 找到该 task 的 herdr_name，用 herdr.prompt_agent(fire-and-forget) 递入答复；
    清掉 awaiting 标记并把状态退回 DISPATCHED——下轮 monitor 看到 worker 恢复/落 outbox 即推进。
    返回 {ok, error?}。协调者据此替用户把答复递进去，用户无需自己找 pane。"""
    gid = board.goal_id
    task = board.tasks.get(task_id)
    if task is None:
        return {"ok": False, "error": f"task {task_id!r} 不存在"}
    if task.status != AWAITING_INPUT:
        return {"ok": False, "error": f"task {task_id!r} 当前状态 {task.status}，非 awaiting_input，无需回答"}
    rt = ledger.load(paths)
    original_keys = set(rt)
    rkey = runtime_key(gid, task_id)
    info = rt.get(rkey, {})
    if info.get("escalated"):
        # 打回触顶 parked 的节点：其 pane 已在升级时被回收，answer 会朝死 pane 注入。
        # 这类不该 answer，应走 reject --force 裁决——明确挡回并给指引。
        return {"ok": False, "escalated": True,
                "error": (f"task {task_id!r} 是打回升级(escalated)待裁决，不能 answer；"
                          f"请用 `reject {gid} <target> --from {task_id} --force` 再给一次机会，或按失败收尾")}
    hname = info.get("herdr_name")
    if not hname:
        return {"ok": False, "error": f"task {task_id!r} 无 herdr_name（pane 已不在？），无法注入答复"}
    try:
        herdr.prompt_agent(hname, text)
    except herdr.HerdrError as e:
        return {"ok": False, "error": f"注入 pane 失败: {e}"}
    now = time.time()
    task.status = DISPATCHED
    task.mark_resumed(now)
    info.pop("awaiting_since", None)
    info.pop("awaiting_reason", None)
    info["last_alive_at"] = now           # 刚喂过 prompt，视为活跃，避免下轮 idle-grace 误杀
    info["dispatched_at"] = now           # 重置存活时钟：答复后按新任务给足硬上限预算
    info["idle_since"] = None
    rt[rkey] = info
    ledger.commit_merge(paths, rt, original_keys)
    board.save(paths)
    ledger.log_event(paths, "task_answered", goal_id=gid, task_id=task_id, agent=task.agent)
    return {"ok": True}


def bypass_tasks(paths: Paths, board: Board, task_ids: list[str],
                 reason: str = "") -> dict[str, Any]:
    """协调者**主动跳过（绕过）**若干步骤：读上游摘要后判定其无需执行，标记为 BYPASSED。

    与失败级联的 SKIPPED 语义相反——BYPASSED 是「有意跳过、视同已满足」：deps_done 把它当
    DONE 计，下游据此转 ready **照常执行**（不级联跳过、不计入 has_failures）。判断由协调者
    （交互会话）依据每个上游任务写回 outbox 的 result_summary 做出，这里只负责落地跳过动作。

    可跳过 PENDING/READY/DISPATCHED/AWAITING_INPUT：在途态（DISPATCHED 在跑 / AWAITING_INPUT
    等人）会先回收其 pane + 清信号 + 剔账本再标记，避免孤儿 pane 泄漏、长期霸占全局容量；
    终态(DONE/FAILED/SKIPPED/BYPASSED)拒绝（无可跳过）。
    返回 {bypassed, not_found, already_terminal, newly_ready, complete, summary}。
    """
    gid = board.goal_id
    reason = reason or "协调者判定无需执行（绕过）"
    out: dict[str, Any] = {"bypassed": [], "not_found": [],
                           "already_terminal": [], "newly_ready": []}
    rt = ledger.load(paths)
    original_keys = set(rt)
    rt_dirty = False
    for tid in task_ids:
        task = board.tasks.get(tid)
        if task is None:
            out["not_found"].append(tid)
            continue
        if task.status in TERMINAL:
            out["already_terminal"].append(tid)   # 已是终态，无可跳过
            continue
        if task.status in INFLIGHT:               # 在途(在跑/等人)的先回收 pane/信号/账本，再绕过
            reclaim.reclaim_task(paths, rt, gid, tid, task.agent)
            rt_dirty = True
            # 在途 worker 可能已往共享 artifacts_dir 写了半成品——归档进 .history（永不删），把它移出当前
            # 目录，避免下游 see_artifacts 继承到一个「已判定无需执行」节点的残留产物。
            dispatch.archive_task_artifacts(paths, gid, task)
            task.close_run("bypassed", time.time())
        task.status = BYPASSED
        task.result_summary = reason
        task.error = None
        out["bypassed"].append(tid)
        ledger.log_event(paths, "task_bypassed", goal_id=gid, task_id=tid, reason=reason)
    if out["bypassed"]:
        before = {t.task_id for t in board.tasks.values() if t.status == READY}
        board.refresh_ready()                       # 绕过后下游可能转 ready（deps 视同满足）
        out["newly_ready"] = sorted(
            t.task_id for t in board.tasks.values()
            if t.status == READY and t.task_id not in before)
        board.save(paths)
    if rt_dirty:
        ledger.commit_merge(paths, rt, original_keys)
    return _with_progress(out, board)


def retask_task(paths: Paths, board: Board, task_id: str, *,
                new_goal: str | None = None, append: str | None = None,
                expect: str | None = None, force: bool = False,
                reason: str = "") -> dict[str, Any]:
    """协调者**推进中改写某步的 goal**：读完上游 outbox 摘要后，为「还没派发的下游」现改指令
    （或加 --force 回收正在跑的那步重派）。与 goal「分派即冻结」互补——改的是**派发前**的 board
    goal（下轮 tick 现取），或**回收后重派**，不是热改在跑 worker。

    作用域：PENDING/READY 直接改；DISPATCHED/AWAITING_INPUT（在途：在跑 / 等人）需 force（先回收
    pane/信号/workspace/账本再重置为待派）；终态(DONE/FAILED/SKIPPED/BYPASSED)拒绝。new_goal 全量
    替换、append 追加，二选一必给一。attempts 不自增（主动改写 ≠ 业务重试，不消耗 max_retries）。
    返回 {retasked, not_found, rejected_terminal, rejected_running, status, new_goal, forced, complete, summary}。
    """
    gid = board.goal_id
    out: dict[str, Any] = {"retasked": [], "not_found": [], "rejected_terminal": [],
                           "rejected_running": [], "status": "", "new_goal": "",
                           "forced": False}
    task = board.tasks.get(task_id)
    if task is None:
        out["not_found"].append(task_id)
        return out
    if task.status in TERMINAL:
        out["rejected_terminal"].append(task_id)   # 终态：产物已定，要重来走重新 plan
        return out
    if new_goal is None and append is None:
        raise ValueError("retask 需给 --goal（全量替换）或 --append（追加）其一")

    # 先算新 goal 并校验，通过后才动状态/回收，避免「回收了 pane 却因 goal 非法半途失败」
    goal = new_goal if new_goal is not None else (task.goal + "\n" + append)
    if not goal.strip():
        raise ValueError("retask 后的 goal 为空")
    if UNFILLED_SLOT_RE.search(goal):   # 残留模板槽：绝不把半成品 goal 发给 worker
        raise ValueError(f"retask 后的 goal 仍有未填的模板槽 {{{{}}}}：{goal[:80]}")

    if task.status in INFLIGHT:
        if not force:
            out["rejected_running"].append(task_id)  # 在途(在跑/等人)：goal 已冻结，需 --force 回收重派或先 skip
            return out
        rt = ledger.load(paths)                     # force：回收在途 pane/信号/workspace/账本
        original_keys = set(rt)
        reclaim.reclaim_task(paths, rt, gid, task_id, task.agent)
        ledger.commit_merge(paths, rt, original_keys)
        task.close_run("retasked", time.time())       # 被回收的那次尝试留痕，不丢耗时
        task.status = PENDING                         # 重置为待派，refresh_ready 依 deps 提升回 READY
        task.attempts = 0                             # 兑现「retask 不消耗 max_retries」：在途 force 回收重派
        out["forced"] = True                          # 是「换了指令重来」而非业务重试，归零 attempts 与 reject 语义对齐

    task.goal = goal
    if expect is not None:
        task.expect = expect
    task.error = None
    board.refresh_ready()
    board.save(paths)
    mode = "replace" if new_goal is not None else "append"
    ledger.log_event(paths, "task_retasked", goal_id=gid, task_id=task_id,
               mode=mode, forced=out["forced"], reason=reason or "")
    out["retasked"].append(task_id)
    out["status"] = task.status
    out["new_goal"] = goal
    return _with_progress(out, board)


def _reset_task_for_rerun(paths: Paths, rt: dict, board: Board, tid: str) -> bool:
    """把某节点重置为待派 PENDING 以便重跑（打回连锁失效用）：在途(dispatched/awaiting)的先回收
    pane/信号/workspace/账本；清 stale 结果字段与业务尝试计数。

    reject 是「上游产出作废、输入变了要重算」，**不是失败重试**——故 attempts/tried_agents 归零、
    不消耗 max_retries（loop guard 由目标节点的 reject_count 单独承担）。返回 rt 是否被改动。"""
    gid = board.goal_id
    task = board.tasks[tid]
    # BYPASSED 是**有意的终态决策**（协调者主动跳过 / fan-out 模板展开后的容器），reject 连锁绝不复活它：
    # 否则被 reject 上游波及时，① 普通被绕过节点会违背「无需执行」语义被动重跑；② 已展开的 fan-out
    # 模板容器会被复位→refresh_ready 又把它转回 AWAITING_EXPANSION，与旧子任务并存、再 expand 撞 id
    # 冲突，令 Job 永久悬挂（is_complete 永不为真）。保持 BYPASSED 终态，只重跑真正跑过的节点即可。
    if task.status == BYPASSED:
        return False
    rt_dirty = task.status in INFLIGHT
    # 在途的回收 pane/workspace/账本；非在途的（done/failed 待重跑）也清掉残留信号
    reclaim.reclaim_task(paths, rt, gid, tid, task.agent, close_pane=rt_dirty, pop=rt_dirty)
    if rt_dirty:
        task.close_run("reset", time.time())   # 在途尝试被打回连锁作废：收尾留痕（runs 历史不清）
    task.status = PENDING
    task.attempts = 0
    task.tried_agents = []
    task.result_summary = ""
    task.artifacts = []
    task.artifact_warnings = []          # 同 artifacts：上一轮的结果快照随重置作废
    task.error = None
    return rt_dirty


def apply_reject_core(paths: Paths, board: Board, rt: dict, target_id: str, *,
                       from_task: str, reason: str, evidence: list[str] | None,
                       enforce_declared: bool = True, force: bool = False) -> dict[str, Any]:
    """打回核心（monitor 自动 + CLI 手动共用）：把 target **连同其全部后代**重置重跑，并把本次
    意见+证据作为凭证累积注入 target；达打回上限则升级给协调者。**只改 board+rt 内存、不落盘、
    不 refresh_ready**——由调用方统一提交，保证与 monitor 共享同一份 rt、避免并发写账本。

    - enforce_declared：worker 自助打回受声明约束（target 必须在 from_task 的 reject_to 里）；
      CLI 手动打回为协调者授权动作，置 False 跳过该校验。
    - force：越过 loop guard（reject_count 上限）强制再打回一次；同时能把已升级 parked 的打回者救活。
    返回 {applied, reset, escalated, not_found, rejected_illegal, target, from, reject_count, rt_dirty}。
    """
    gid = board.goal_id
    out: dict[str, Any] = {"applied": [], "reset": [], "escalated": False,
                           "not_found": [], "rejected_illegal": [], "invalid_from": False,
                           "rejected_terminal": [], "escalate_no_owner": False,
                           "target": target_id,
                           "from": from_task, "reject_count": 0, "rt_dirty": False}
    target = board.tasks.get(target_id)
    if target is None:
        out["not_found"].append(target_id)
        return out
    # 与 skip(bypass)/retask 的终态守卫对齐：不能打回 BYPASSED（有意跳过 / 已展开的 fan-out 容器）或
    # SKIPPED（上游失败连累、不可达）——它们没有「已产出、可重做」的成果，复位只会把容器复活成孤立
    # AWAITING_EXPANSION 令 Job 假死。DONE/FAILED 仍可打回（reject 本意就是重跑真正跑过的节点）。
    if target.status in (BYPASSED, SKIPPED):   # 无产出可重做的终态（DONE/FAILED 仍可打回）
        out["rejected_terminal"].append(target_id)
        return out
    rejecter = board.tasks.get(from_task)
    if enforce_declared and (rejecter is None or target_id not in rejecter.reject_to):
        out["rejected_illegal"].append(target_id)   # 未声明可打回该目标：拒绝，不动流程
        return out
    if not enforce_declared and from_task and rejecter is None:
        # 手动打回给了 --from 但它不是本 Job 的任务：拒绝。否则脏来源会被原样写进永久凭证
        # (target.rejections[].from)，且在触顶分支导致「无主静默升级」(见下)。
        out["invalid_from"] = True
        return out
    limit = target.max_rejects or MAX_REJECTS
    if not force and target.reject_count >= limit:
        # loop guard 触顶 → 升级给协调者：不再自动回退，把打回者 parked 为 AWAITING_INPUT(escalated)
        # （非终态、不算失败），回收其 pane、清 outbox 防被重复读成又一次打回，直到 CLI 介入。
        out["escalated"] = True
        out["reject_count"] = target.reject_count
        if rejecter is not None:
            rinfo = reclaim.reclaim_task(paths, rt, gid, from_task, rejecter.agent, pop=False)
            rejecter.status = AWAITING_INPUT
            rejecter.error = (f"reject-exhausted: 目标 {target_id} 已被打回 {target.reject_count} 次"
                              f"（上限 {limit}）仍未通过，需人工裁决")
            rinfo["escalated"] = True
            rinfo["awaiting_since"] = time.time()
            rinfo["awaiting_reason"] = (
                f"[打回已达上限] 目标 {target_id} 被打回 {target.reject_count} 次仍未通过。"
                f"最新意见：{reason}。请裁决：`reject {gid} {target_id} --from {from_task} --force` "
                f"再给一次机会（可先 retask {target_id} 加强指导），或按失败收尾。")
            rt[runtime_key(gid, from_task)] = rinfo
            out["rt_dirty"] = True
        else:
            # 触顶但没有可 park 的打回者（手动打回未给 --from）：不能返回 escalated=True 却无人
            # 负责、target 也不重跑——那样流程会静默冻结在 target。显式标记需人工决断，让 CLI 非零退出。
            out["escalate_no_owner"] = True
        ledger.log_event(paths, "reject_exhausted", goal_id=gid, task_id=target_id,
                   from_task=from_task, reject_count=target.reject_count, limit=limit,
                   reason=reason)
        return out
    # 正常打回：累积凭证 → 重置 target 及其全部后代（含打回者自身）为待派，下轮从 target 起重跑。
    # 证据先快照进 target 名下 .rejections/（归档豁免区），避免打回者重跑归档后原证据路径失效。
    stable_evidence = _stash_reject_evidence(paths, gid, target_id,
                                             target.reject_count, evidence)
    target.rejections.append({"from": from_task, "reason": reason,
                              "evidence": stable_evidence, "ts": time.time()})
    target.reject_count += 1
    out["reject_count"] = target.reject_count
    reset_ids = [target_id] + sorted(board.descendants(target_id))
    for tid in reset_ids:
        if _reset_task_for_rerun(paths, rt, board, tid):
            out["rt_dirty"] = True
    out["applied"].append(target_id)
    out["reset"] = reset_ids
    ledger.log_event(paths, "task_rejected", goal_id=gid, task_id=target_id,
               from_task=from_task, reject_count=target.reject_count,
               reset=reset_ids, forced=force, reason=reason)
    return out


def apply_reject(paths: Paths, board: Board, target_id: str, *,
                 from_task: str = "", reason: str = "", evidence: list[str] | None = None,
                 force: bool = False) -> dict[str, Any]:
    """协调者**手动打回**（CLI 入口）：读某节点结果后，把 target 连同其全部后代重置重跑、注入凭证。
    手动为授权动作，跳过「声明约束」校验；默认仍受 loop guard 约束，--force 越过上限并救活已升级节点。
    返回核心结果 + complete/summary。"""
    rt = ledger.load(paths)
    original_keys = set(rt)
    reason = reason or "协调者手动打回"
    out = apply_reject_core(paths, board, rt, target_id, from_task=from_task,
                             reason=reason, evidence=evidence,
                             enforce_declared=False, force=force)
    if out.get("applied") or out.get("escalated"):
        board.refresh_ready()
        board.save(paths)
    if out.get("rt_dirty"):
        ledger.commit_merge(paths, rt, original_keys)
    return _with_progress(out, board)


def expand_fanout(paths: Paths, board: Board, template_id: str,
                  children_goals: list[str], children_expects: list[str] | None = None,
                  reason: str = "") -> dict[str, Any]:
    """协调者**动态展开** fan-out 模板节点：把它展开成 len(children_goals) 个子任务并重接下游依赖。

    与 bypass/retask/reject 一样是协调者运行期改 board 的动作：模板节点 deps 满足后停在
    AWAITING_EXPANSION（tick 每轮上报），协调者读上游 outbox 决定 K、由 CLI 把每片 goal（{{shard}}/
    {{total}} 已替换）传进来。这里做结构展开 → 重校验 DAG → refresh_ready → 落盘 → 记事件。
    子任务同组共享 fanout_max_parallel 并发闸（见 dispatch.run_dispatch_round），宽度不限、只限同时并发。
    返回 board.expand_fanout 的结果 + complete/summary。展开后 DAG 非法则不落盘、返回错误。
    """
    # 漏填守卫下沉到协调者层（与 plan 载入、retask 同级的不变量）：任何驱动方（含绕过 CLI 直调者）
    # 都不得把残留 {{槽}} 的半成品 goal 派给 worker。CLI 已替换 {{shard}}/{{total}}，这里兜底再查一次。
    leftover = sorted({m for g in children_goals for m in UNFILLED_SLOT_RE.findall(g)})
    if leftover:
        return {"ok": False,
                "error": f"子任务 goal 仍有未填占位槽 {leftover}——不得把半成品 goal 派给 worker",
                "children": [], "rewired": []}
    res = board.expand_fanout(template_id, children_goals, children_expects)
    if not res.get("ok"):
        return _with_progress(res, board)
    try:
        board.validate()                 # 重接线后重校验：悬空/环/see_artifacts⊆needs/reject_to⊆祖先
    except DagError as e:
        ledger.log_event(paths, "expand_invalid", goal_id=board.goal_id, task_id=template_id,
                   error=str(e))
        return {"ok": False, "error": f"展开后 DAG 非法（未落盘）：{e}",
                "children": [], "rewired": []}
    board.refresh_ready()                # 子任务 deps 已满足 → 转 READY，下轮受组闸约束派发
    board.save(paths)
    ledger.log_event(paths, "task_expanded", goal_id=board.goal_id, task_id=template_id,
               count=len(res["children"]), children=res["children"],
               rewired=res["rewired"], reason=reason or "")
    return _with_progress(res, board)
