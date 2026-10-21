"""组装发给 worker 的任务信号（inbox）与引导语。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import signals, pane_scan
from .config import Paths, resolve_task_workspace
from .models import Task, AgentManifest


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
            "证据文件为绝对路径，按需直接读取核对。\n")
    # 打回能力：本节点是评审/校验节点、声明了可打回的上游 → 告知如何写打回信号。
    reject_cap_note = ""
    if task.reject_to:
        reject_cap_note = (
            f"你有**打回**权限：若评审/校验后判定上游 {task.reject_to} 的产出确有问题，"
            f"可在 outbox 写 status=reject、reject_to=<其中一个上游 task_id>、"
            f"summary=具体打回意见、artifacts=证据文件（绝对路径）。框架会把该上游连同其下游"
            f"全部重置重跑、并把你的意见+证据作为凭证注入它，随后你会被重新拉起复评。"
            f"打回有次数上限，请只在确有实质问题时打回，意见要具体可操作。\n")
    return (
        f"你是 team 的 {m.role or m.name}。{skills}\n"
        f"1) 读取任务信号：{inbox}\n"
        f"2) 你的角色说明：{persona}\n"
        f"3) 按信号里的 goal 执行；只做该任务，不扩大范围。\n"
        f"4) 完成或失败后，把结构化结果写入：{outbox}\n"
        f"   字段：task_id, status(done|failed), summary, artifacts, error。\n"
        f"   **该文件必须是纯 JSON**：不要包 ```围栏、不要前后加解释文字；"
        f"summary 里若含引号/换行/代码请正确转义（用能序列化 JSON 的方式写，别手拼），"
        f"长内容放产物文件、summary 只写摘要。写坏会被判「交付损坏」而误判失败。\n"
        f"{retry_note}"
        f"{reject_history_note}"
        f"{reject_cap_note}"
        f"工作目录：你的 cwd 是本任务独占的临时空间 {cwd} —— 本任务的临时/中间文件"
        f"**默认写在这个目录内**。上级基座 workspace（含 CLAUDE.md/.claude）**默认只作只读参考**；"
        f"**但若本任务的 goal 明确要求在基座 workspace 这一层写入/积累数据（如沉淀可复用资料、追加知识库），"
        f"则按 goal 指定的路径写入该层是允许的**（这类持久化积累不受「只写独占目录」限制）——"
        f"未被明确要求时，仍不要改动基座里的文件。跨任务交付的产物写到下面的绝对 artifacts 目录。\n"
        f"注意：本任务的产物目录（绝对路径，跨任务交接的产物也写这里，下游按上报路径读取）：{art}\n"
        f"   **所有产物必须写到这个绝对目录下**；即使 goal 文本里写的是相对的 "
        f"'shared/artifacts/<文件>'，也要落到上面这个绝对目录，不要写到别处的扁平 shared/artifacts。\n"
        f"   信号里的 context_refs / artifacts_dir 均为绝对路径，直接用即可，勿再拼接。\n"
        f"   信号里若有 inherited_artifacts，是你依赖的指定上游节点产出的文件"
        f"（每项含 from=来自哪个节点、files=绝对路径列表），按需读取以获取上游成果。\n"
        f"   若判定本任务为**永久性失败**（如注入/参数非法/前置缺失、重试也不会好），"
        f"请在 outbox 里加 retryable=false，框架将跳过无谓重试。\n"
        f"若**确实需要人来拍板**才能继续（如遇到未决的 [blocking] 决定、必须由人确认的高风险动作），"
        f"可在终端打印一行 `{pane_scan.AWAIT_SENTINEL} <你的问题>` 然后停下等待——协调者会识别为「等人介入」"
        f"（而非挂死），把问题转给人；拿到答复后你会在同一会话里收到回复，据此继续并最终写 outbox。"
        f"不要为无关紧要的小选择滥用它——能按角色合理默认就默认，别动辄挂起。\n"
        f"写完 outbox 即视为交付。"
    )
