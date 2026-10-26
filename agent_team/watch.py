"""后台收割守护（`team watch`）：协调者不在场时也持续收割 outbox、判超时、回收 pane。

只做 monitor 里**确定性**的那部分（收割 / 存活与超时判定 / 回收 / 级联落盘），**从不分派**：
「读上游摘要决定 skip/retask/expand、再派下游」仍归协调者。这样会话结束、上下文压缩、人走开
期间，写完的 outbox 不再一直占着 pane 与 agent 容量，超时也按时判、不会拖到下次有人来才发现。

与协调者并发安全靠 board.job_lock：每个 Job 锁内重新载入再收割，一次只持一把锁。
全局单实例（state/watch.lock 非阻塞独占锁）。为不泄漏常驻进程，三条退出线兜底：
连续 idle_exit 秒无可收割任务、连续 MAX_CONSECUTIVE_ERRORS 轮出错、存活满 MAX_LIFETIME。
退出不丢任何东西——下次 tick 有真实分派时由 ensure_running 再拉起。
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

from . import ledger
from .config import Paths, load_team

try:
    import fcntl
except ImportError:         # pragma: no cover - 仅 Windows 命中
    fcntl = None            # type: ignore[assignment]

log = logging.getLogger(__name__)

# 收割间隔(秒)：与 wait 的 poll 同量级。
WATCH_POLL = 5.0
# 连续多久无在途任务就退出（秒）：Job 收敛/全停后不常驻。
WATCH_IDLE_EXIT = 300.0
# 连续多少轮收割出错就退出：持续出错（如状态目录坏了）时别空转到天荒地老，留给下次 tick 重拉。
MAX_CONSECUTIVE_ERRORS = 20
# 单个守护进程的存活上限（秒）：任何意外都不至于常驻；超时退出后下次 tick 重拉，顺带换上新代码。
MAX_LIFETIME = 12 * 3600.0
# 设了这个环境变量（非空）就不自动拉起守护——测试、或用户想完全手动驱动时用。
NO_WATCH_ENV = "TEAM_NO_WATCH"


def _lock_path(paths: Paths):
    return paths.state_dir / "watch.lock"


def enabled(paths: Paths) -> bool:
    """team.yaml `watch.enabled`，默认开；环境变量 TEAM_NO_WATCH 非空则关。"""
    if os.environ.get(NO_WATCH_ENV):
        return False
    cfg = load_team(paths).get("watch") or {}
    return not (isinstance(cfg, dict) and cfg.get("enabled") is False)


@contextlib.contextmanager
def _single_instance(paths: Paths) -> Iterator[bool]:
    """非阻塞抢 watch.lock：抢到 yield True（持有至退出），已有实例在跑 yield False。"""
    if fcntl is None:
        yield True
        return
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(paths), "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def is_running(paths: Paths) -> bool:
    with _single_instance(paths) as got:
        return not got


def run(paths: Paths, poll: float = WATCH_POLL, idle_exit: float = WATCH_IDLE_EXIT,
        max_rounds: int | None = None, max_lifetime: float = MAX_LIFETIME) -> dict:
    """前台跑守护循环直到触发退出线（见模块文档；max_rounds 供测试用）。"""
    from .coordinator import harvest_round   # 延迟导入：coordinator 会导入本模块
    with _single_instance(paths) as got:
        if not got:
            return {"status": "already_running"}
        ledger.log_event(paths, "watch_started", pid=os.getpid())
        rounds = harvested = errors = 0
        started = time.monotonic()
        idle_since: float | None = None
        status = "idle_exit"
        try:
            while max_rounds is None or rounds < max_rounds:
                rounds += 1
                try:
                    r = harvest_round(paths)
                    errors = 0
                except Exception:          # noqa: BLE001 —— 守护是旁路，单轮出错只记日志、下轮再来
                    log.exception("watch 收割一轮失败（已忽略）")
                    errors += 1
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        status = "error_exit"
                        break
                    r = {"inflight": True, "changed": {}}
                harvested += sum(len(v) for v in r["changed"].values())
                now = time.monotonic()
                if now - started >= max_lifetime:
                    status = "max_lifetime"
                    break
                if r["inflight"]:
                    idle_since = None
                else:
                    idle_since = idle_since if idle_since is not None else now
                    if now - idle_since >= idle_exit:
                        break
                time.sleep(poll)
            else:
                status = "max_rounds"
        except KeyboardInterrupt:
            status = "interrupted"
        ledger.log_event(paths, "watch_stopped", pid=os.getpid(), reason=status,
                         rounds=rounds, harvested=harvested)
        return {"status": status, "rounds": rounds, "harvested": harvested}


def ensure_running(paths: Paths) -> bool:
    """没有守护在跑就脱离当前会话拉起一个（`python -m agent_team watch`），返回是否新拉起。

    start_new_session：协调者会话结束、shell 被杀也不连带杀掉它——这正是守护存在的意义。
    继承当前环境（HERDR_SESSION / 当前 pane id），收割与回收 pane 时落到同一个 herdr 会话、
    也照样避开协调者自己的 pane。best-effort：拉起失败只记日志。"""
    if not enabled(paths) or is_running(paths):
        return False
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        with open(paths.state_dir / "watch.log", "a", encoding="utf-8") as out:
            proc = subprocess.Popen([sys.executable, "-m", "agent_team", "watch"], cwd=paths.root,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                    start_new_session=True)
        # 拉起方若是长驻进程（run 循环、脚本），守护退出后得有人 wait 它，否则留僵尸进程。
        # daemon 线程：拉起方是一次性 CLI 时随其退出而消失，守护被 init 接管、照样不留僵尸。
        threading.Thread(target=proc.wait, daemon=True).start()
        return True
    except OSError:
        log.exception("拉起 watch 守护失败（已忽略，不影响调度）")
        return False
