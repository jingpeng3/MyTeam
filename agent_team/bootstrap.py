"""组装发给 worker 的任务信号（inbox）与引导语。
"""
from __future__ import annotations

import os
from pathlib import Path
from string import Template

from . import signals, pane_scan
from .config import Paths, resolve_task_workspace
from .models import Task, AgentManifest

# worker 通用交付协议（读 inbox → 执行 → 写 outbox、产物目录、等人介入）的唯一事实源：
# agent 的 prompt.md 只写本角色的工作内容，不再各自抄一份完成协议。
_PROTOCOL = Path(__file__).with_name("worker_protocol.md")


def resolve_refs(paths: Paths, goal_id: str, refs: list[str]) -> list[str]:
    """把 `shared/artifacts/<file>` 形式的引用解析为**本 Job 产物目录下的绝对路径**。

    修复跨任务交接的隐患：worker 的 CWD 是 `agents/<name>/workspace`，而非项目根；旧实现只把
    ref 改写成带 gid 的**相对**串（`shared/artifacts/<gid>/<file>`），从 worker 视角解析会落到
    `agents/<name>/workspace/shared/artifacts/...`——根本不存在。这里直接给绝对路径，
    让「生产者按 goal 写、消费者按 ref 读」指向同一真实文件，不再寄托于 worker 自行拼路径。
    """
    prefix = "shared/artifacts/"
    art = paths.job_artifacts_dir(goal_id)   # Job 产物根（绝对路径），其下按任务分层 <tid>/
    flat = Path(paths.root) / "shared" / "artifacts"   # 扁平（无 gid）产物根
    out = []
    for r in refs:
        if r.startswith(prefix):
            rest = r[len(prefix):]
            if rest.startswith(goal_id + "/"):
                rest = rest[len(goal_id) + 1:]
            gid_path = art / rest
            resolved: Path | None = None
            if gid_path.exists():
                # 显式 <tid>/<文件>（分层新布局）或旧的 Job 根扁平 <文件>——存在即用。
                resolved = gid_path
            elif "/" not in rest:
                # 裸文件名 + Job 根无此文件：产物已分层进各任务子目录，探 <gid>/<tid>/<文件>。
                # 排除 .history 归档，只认各任务「当前」目录；命中多个取字典序第一（稳定可复现）。
                hits = [h for h in sorted(art.glob(f"*/{rest}"))
                        if ".history" not in h.parts]
                if hits:
                    resolved = hits[0]
            # round-5 flow-22 实证：worker 有时不遵循 inbox 的绝对 artifacts_dir，把产物写到
            # 扁平 shared/artifacts/<file>（无 gid 子目录）。gid/分层路径都打不开时回退扁平，
            # 让「作者没强制 worker 写 artifacts_dir」也不至于让 join 伪失败白耗重试。
            if resolved is None and (flat / rest).exists():
                resolved = flat / rest
            out.append(str(resolved if resolved is not None else gid_path))
        elif not os.path.isabs(r) and (Path(paths.root) / r).exists():
            out.append(str(Path(paths.root) / r))   # 其它项目内相对引用也锚到项目根
        else:
            out.append(r)
    return out


def write_inbox_signal(paths: Paths, agent_name: str, gid: str,
                           task: Task, done_deps: list[str],
                           inherited: list[dict] | None = None) -> None:
    """清旧信号 → 组装任务信号（绝对化 context_refs 与 artifacts_dir）→ 写 inbox。

    provisioning 真实路径与 dry-run 预览路径此前各写了一份完全相同的信号组装逻辑，
    收敛到一处，避免两条派发路径的信号 schema 漂移。

    inherited：按 task.see_artifacts 现算的上游产物快照（主线程 board 只读处算好后传入，
    保持 provisioning 线程安全——本函数不碰 board）。
    """
    signals.clear_task_signals(paths, agent_name, gid, task.task_id)
    signal = task.to_signal(done_deps, inherited)
    signal["context_refs"] = resolve_refs(paths, gid, signal.get("context_refs", []))
    signal["artifacts_dir"] = str(paths.task_artifacts_dir(gid, task.task_id))   # 绝对路径，按任务分层
    signals.write_inbox(paths, agent_name, gid, signal)


def build_bootstrap(paths: Paths, m: AgentManifest, goal_id: str, task: Task) -> str:
    """发给 worker 的引导语：明确它去读哪个 inbox、干完写哪个 outbox。"""
    fname = signals.signal_filename(goal_id, task.task_id)
    inbox = paths.inbox(m.name) / fname
    outbox = paths.outbox(m.name) / fname
    persona = Path(m.dir) / m.prompt
    art = paths.task_artifacts_dir(goal_id, task.task_id)
    cwd = resolve_task_workspace(m, goal_id, task.task_id)   # worker 本任务独占 cwd
    skills = f"可用 skill：{', '.join(m.skills)}。" if m.skills else ""
    # 重试时告知这是第几次尝试，让 worker 对非幂等副作用（如 append 类产物）先自查已有产物，
    # 避免「误重试 → 从头重跑 → 产物重复行」的静默数据污染。
    retry_note = ""
    if task.attempts >= 1:
        retry_note = (f"注意：这是本任务的第 {task.attempts + 1} 次尝试；"
                      f"若目标产物已部分存在，请先核对再幂等处理，勿重复追加。\n")
    # 打回凭证：本任务曾被下游打回过 → 把历次意见+证据醒目注入，令 worker 针对性修复、勿重复犯错。
    reject_history_note = ""
    if task.rejections:
        lines = []
        for i, rj in enumerate(task.rejections, 1):
            frm = rj.get("from", "?")
            reason = rj.get("reason", "")
            ev = rj.get("evidence") or []
            ev_str = ("；证据文件：" + ", ".join(ev)) if ev else ""
            lines.append(f"  第{i}次[来自 {frm}]：{reason}{ev_str}")
        reject_history_note = (
            f"⚠ 本任务此前已被下游打回 {len(task.rejections)} 次。历次打回意见与证据如下——"
            f"请逐条针对性修复，**务必不要重复这些问题**：\n"
            + "\n".join(lines) + "\n"
            "证据文件为绝对路径，按需直接读取核对。针对意见整改，保留已达标的部分，不要推倒重来。\n")
    # 打回能力：本节点是评审/校验节点、声明了可打回的上游 → 告知如何写打回信号。
    reject_cap_note = ""
    if task.reject_to:
        reject_cap_note = (
            f"你有**打回**权限：若评审/校验后判定上游 {task.reject_to} 的产出确有问题，"
            f"可在 outbox 写 status=reject、reject_to=<其中一个上游 task_id>、"
            f"summary=具体打回意见、artifacts=证据文件（绝对路径）。框架会把该上游连同其下游"
            f"全部重置重跑、并把你的意见+证据作为凭证注入它，随后你会被重新拉起复评。"
            f"打回有次数上限，请只在确有实质问题时打回，意见要具体可操作。\n")
    protocol = Template(_PROTOCOL.read_text(encoding="utf-8")).substitute(
        inbox=inbox, outbox=outbox, persona=persona, cwd=cwd, artifacts_dir=art,
        task_notes=retry_note + reject_history_note + reject_cap_note,
        await_sentinel=pane_scan.AWAIT_SENTINEL,
    )
    return f"你是 team 的 {m.role or m.name}。{skills}\n" + protocol.rstrip("\n")
