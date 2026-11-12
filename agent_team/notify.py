"""Job 级关键节点提醒：流程开始 / 需人介入 / 打回 / 流程结束时弹一条 herdr 通知。

只在这几处 Job 级节点提醒，不跟随单个 worker 的完成。复盘 Job（retro-*）是框架自动拉起的，不提醒。
team.yaml `notify: {enabled: false}` 关闭。通知纯属提示，任何失败都静默。
"""
from __future__ import annotations

from . import herdr
from .config import Paths, load_team

_RETRO_PREFIX = "retro-"      # 与 retro.RETRO_PREFIX 一致；不导入 retro，免得 board → retro 循环导入


def enabled(paths: Paths) -> bool:
    try:
        cfg = load_team(paths).get("notify") or {}
    except Exception:          # noqa: BLE001 —— team.yaml 读坏了也不该让提醒拖累调度
        return False
    return not (isinstance(cfg, dict) and cfg.get("enabled") is False)


def _send(paths: Paths, goal_id: str, title: str, body: str, sound: str) -> None:
    if goal_id.startswith(_RETRO_PREFIX) or not enabled(paths):
        return
    herdr.notify(f"{title} · {goal_id}", body[:200], sound=sound)


def job_started(paths: Paths, goal_id: str, summary: str) -> None:
    _send(paths, goal_id, "流程开始", summary, "none")


def job_finished(paths: Paths, goal_id: str, summary: str, ok: bool) -> None:
    _send(paths, goal_id, "流程结束" if ok else "流程结束（有失败）", summary, "done")


def needs_input(paths: Paths, goal_id: str, task_id: str, question: str) -> None:
    _send(paths, goal_id, "需要人介入", f"{task_id}: {question}", "request")


def rejected(paths: Paths, goal_id: str, task_id: str, from_task: str, reason: str,
             exhausted: bool = False) -> None:
    if exhausted:
        _send(paths, goal_id, "打回触顶，需人裁决", f"{task_id}（来自 {from_task or '协调者'}）: {reason}",
              "request")
    else:
        _send(paths, goal_id, "打回", f"{task_id}（来自 {from_task or '协调者'}）: {reason}", "none")
