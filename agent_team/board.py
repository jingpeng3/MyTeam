"""任务板 + DAG：状态在文件、不在内存，支持协调者会话被压缩后恢复。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .config import Paths, atomic_write_json, file_lock, read_json_or
from .models import (Task, PENDING, READY, DISPATCHED, DONE, FAILED, SKIPPED, BYPASSED,
                     AWAITING_EXPANSION, TERMINAL, INFLIGHT, FAILURE, SATISFIED)

log = logging.getLogger(__name__)


class DagError(ValueError):
    """plan 结构非法：悬空依赖 / 成环 / 重复 task_id。载入期即报错，不拖到运行期。"""



class Board:
    """持有一个目标的任务 DAG 及其运行态，落盘在 coordinator/state/jobs/<goal_id>.json。"""

    def __init__(self, goal_id: str, goal: str, tasks: list[Task]):
        self.goal_id = goal_id
        self.goal = goal
        self.tasks: dict[str, Task] = {t.task_id: t for t in tasks}
        self._version = 0   # 单调版本号：save 时比对磁盘，检测同 Job 并发驱动的 lost-update
        # Job 级计时：created_at=plan 载入时刻（旧 board 无此字段则为 None）；completed_at=首次收敛时刻，
        # 被打回/重置重新打开时清空（见 _stamp_completion）。
        self.created_at: float | None = time.time()
        self.completed_at: float | None = None

    @property
    def version(self) -> int:
        """最近一次载入/落盘时的单调版本号（只读；由 save 推进）。"""
        return self._version

    # ---------- 持久化 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "goal": self.goal,
            "version": self._version,
            "updated": time.time(),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Board:
        tasks = [Task.from_dict(td) for td in d.get("tasks", [])]
        b = cls(d["goal_id"], d.get("goal", ""), tasks)
        b._version = int(d.get("version", 0))
        b.created_at = d.get("created_at")
        b.completed_at = d.get("completed_at")
        return b

    def _stamp_completion(self) -> None:
        """收敛即记 completed_at（只记首次）；Job 又被打开（打回/retask 让节点回到非终态）则清空。"""
        if self.is_complete():
            if self.completed_at is None:
                self.completed_at = time.time()
        else:
            self.completed_at = None

    def wall_s(self, now: float) -> float | None:
        """Job 墙钟：created_at → completed_at（未完成按 now）；旧 board 无 created_at 返回 None。"""
        if self.created_at is None:
            return None
        return round((self.completed_at or now) - self.created_at, 3)

    def save(self, paths: Paths) -> Path:
        f = paths.job_file(self.goal_id)
        self._stamp_completion()
        # 全程持锁做「读磁盘版本 → 比对 → 原子写」：崩溃不留半截 board，并发驱动能察觉冲突。
        with file_lock(f):
            on_disk = int(read_json_or(f, {}).get("version", 0))
            if on_disk > self._version:
                # 另一驱动者(会话/后台 run)已推进了同一 Job。last-writer-wins 但保持版本单调，
                # 并留一条告警——同 Job 不应被并发驱动（见 CLAUDE.md：改用只读 status/report 看进度）。
                log.warning(
                    "board %s 检测到并发写：磁盘版本 %d > 本地载入 %d，可能覆盖对方状态更新",
                    self.goal_id, on_disk, self._version,
                )
            self._version = max(on_disk, self._version) + 1
            atomic_write_json(f, self.to_dict())
        return f

    @classmethod
    def load(cls, paths: Paths, goal_id: str) -> Board | None:
        f = paths.job_file(goal_id)
        if not f.exists():
            return None
        # 容错读：外部篡改/截断/磁盘异常产生的坏 Job 文件返回 None（告警），不抛异常。
        # 否则单个坏文件会经 load_all 拖垮 jobs/report/tick_all/dashboard 的全体 Job 视图。
        # 与同模块 save 侧的 read_json_or、config.read_json_or 的容错口径一致。
        d = read_json_or(f, None)
        if not isinstance(d, dict) or "goal_id" not in d:
            log.warning("Job 文件损坏或结构非法，已跳过：%s", f)
            return None
        return cls.from_dict(d)

    @staticmethod
    def list_job_ids(paths: Paths) -> list[str]:
        if not paths.jobs_dir.exists():
            return []
        return sorted(p.stem for p in paths.jobs_dir.glob("*.json"))

    @classmethod
    def load_all(cls, paths: Paths) -> list[Board]:
        return [b for gid in cls.list_job_ids(paths)
                if (b := cls.load(paths, gid)) is not None]

    def delete(self, paths: Paths) -> None:
        f = paths.job_file(self.goal_id)
        if f.exists():
            f.unlink()

    # ---------- DAG 逻辑 ----------
    def validate(self) -> None:
        """载入期结构校验：悬空 needs / 成环，命中即抛 DagError。

        对照旧行为：deps_done 用 `if n in self.tasks` 会把悬空依赖静默当"已满足"，
        成环则永远无 ready 节点、只表现为一句模糊的 stuck。这里把错误挡在执行之前。
        """
        # 1) needs 必须指向已声明的 task_id
        dangling = {t.task_id: [n for n in t.needs if n not in self.tasks]
                    for t in self.tasks.values()}
        dangling = {k: v for k, v in dangling.items() if v}
        if dangling:
            detail = "; ".join(f"{k} → 未知依赖 {v}" for k, v in dangling.items())
            raise DagError(f"悬空依赖：{detail}")
        # 1b) see_artifacts 必须 ⊆ needs：只能继承「你确实依赖、因而必然先完成」的上游产物，
        #     否则那个节点可能还没跑、artifacts 为空，继承语义无法保证。载入期即挡下。
        bad_inherit = {t.task_id: [n for n in t.see_artifacts if n not in t.needs]
                       for t in self.tasks.values()}
        bad_inherit = {k: v for k, v in bad_inherit.items() if v}
        if bad_inherit:
            detail = "; ".join(f"{k} → see_artifacts {v} 不在其 needs 里" for k, v in bad_inherit.items())
            raise DagError(f"非法产物继承（see_artifacts 必须 ⊆ needs）：{detail}")
        # 1c) reject_to 必须 ⊆ 本节点的**传递上游（祖先闭包）**：只能打回你消费过其产出的上游。
        #     打回是运行期回退边——回退起点必须在你之前跑过，否则「重跑上游」无从谈起。载入期即挡下。
        bad_reject = {t.task_id: [n for n in t.reject_to if n not in self.ancestors(t.task_id)]
                      for t in self.tasks.values()}
        bad_reject = {k: v for k, v in bad_reject.items() if v}
        if bad_reject:
            detail = "; ".join(f"{k} → reject_to {v} 不是其传递上游" for k, v in bad_reject.items())
            raise DagError(f"非法打回目标（reject_to 必须 ⊆ 传递上游/祖先）：{detail}")
        # 2) Kahn 拓扑排序检测环
        indeg = {tid: 0 for tid in self.tasks}
        for t in self.tasks.values():
            for _ in t.needs:
                indeg[t.task_id] += 1
        queue = [tid for tid, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for t in self.tasks.values():
                if cur in t.needs:
                    indeg[t.task_id] -= 1
                    if indeg[t.task_id] == 0:
                        queue.append(t.task_id)
        if seen != len(self.tasks):
            cyc = [tid for tid, d in indeg.items() if d > 0]
            raise DagError(f"检测到环，涉及节点：{sorted(cyc)}")

    def deps_done(self, t: Task) -> bool:
        # DONE 与 BYPASSED 都算「依赖已满足」：BYPASSED 是协调者主动绕过、视同完成，
        # 下游据此照常推进（区别于 FAILED/SKIPPED 会连累下游级联跳过）。
        return all(self.tasks[n].status in SATISFIED
                   for n in t.needs if n in self.tasks)

    def blockers(self, t: Task) -> list[str]:
        """t 的直接上游里已失败/被跳过（令 t 不可达）的 task_id。"""
        return [n for n in t.needs if n in self.tasks and self.tasks[n].status in FAILURE]

    def refresh_ready(self) -> list[str]:
        """把依赖已满足的 pending 节点标为 ready；并把上游已失败/跳过的 pending 级联标为 SKIPPED。

        级联跳过修复了「上游 FAILED 时下游永远 pending → is_complete 永不为真 → 只能报 stuck」
        的收尾语义缺陷：不可达节点进入 SKIPPED 终态，Job 得以带失败正常收敛。

        返回**本次调用新变成 SKIPPED** 的 task_id 列表，供上层补记可观测事件（board 层本身无日志句柄）。
        """
        newly_skipped: list[str] = []
        # 1) 级联跳过：任一前驱为 FAILED/SKIPPED 的 pending 节点不可达 → SKIPPED（传播到底）
        changed = True
        while changed:
            changed = False
            for t in self.tasks.values():
                if t.status != PENDING:
                    continue
                blockers = self.blockers(t)
                if blockers:
                    t.status = SKIPPED
                    if t.error is None:
                        # 透传根因：直接上游若本身是被跳过的，继承它的 root，让深层级联一眼看到源头
                        t.error = (f"upstream {blockers} failed/skipped"
                                   f" (root={self.root_failure(t)})")
                    newly_skipped.append(t.task_id)
                    changed = True
        # 2) 依赖全部 DONE 的 pending → ready；但 fan-out 模板节点**不直接派发**，转
        #    AWAITING_EXPANSION 等协调者按上游产出调 expand 展开成子任务（见 models.Task.fanout）。
        now = time.time()
        for t in self.tasks.values():
            if t.status == PENDING and self.deps_done(t):
                t.status = AWAITING_EXPANSION if t.fanout else READY
                if t.status == READY:
                    t.ready_at = now      # 排队起点：派发时 open_run 据此算 queue_s
        return newly_skipped

    def root_failure(self, t: Task) -> str | None:
        """回溯 needs 找到导致本节点不可达的**最初 FAILED 祖先** task_id。

        级联下游只知道直接上游，排障要逐级手翻；这里一次回溯到真正的失败源。
        """
        seen: set[str] = set()
        stack = list(t.needs)
        while stack:
            n = stack.pop()
            if n in seen or n not in self.tasks:
                continue
            seen.add(n)
            up = self.tasks[n]
            if up.status == FAILED:
                return n
            if up.status == SKIPPED:
                stack.extend(up.needs)
        return None


    def ancestors(self, tid: str) -> set[str]:
        """tid 的传递上游闭包（沿 needs 回溯到底，不含自身）。用于 reject_to 合法性校验。"""
        seen: set[str] = set()
        stack = list(self.tasks[tid].needs) if tid in self.tasks else []
        while stack:
            n = stack.pop()
            if n in seen or n not in self.tasks:
                continue
            seen.add(n)
            stack.extend(self.tasks[n].needs)
        return seen

    def descendants(self, tid: str) -> set[str]:
        """tid 的传递下游闭包（凡直接/间接 needs 到 tid 的节点，不含自身）。

        打回时用：被打回目标的产出既已作废，其**全部后代**（含打回者自身）都基于旧产出、必须重算。
        """
        seen: set[str] = set()
        stack = [t.task_id for t in self.tasks.values() if tid in t.needs]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(t.task_id for t in self.tasks.values() if n in t.needs)
        return seen

    def ready_tasks(self) -> list[Task]:
        self.refresh_ready()
        return [t for t in self.tasks.values() if t.status == READY]

    def dispatched_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == DISPATCHED]

    def pollable_tasks(self) -> list[Task]:
        """monitor 每轮要复查的在途任务：dispatched（等 outbox/存活）+ awaiting_input（等人回答）。
        AWAITING_INPUT 也要持续复查——人答复后要能观测到 worker 恢复/落 outbox 并推进。
        AWAITING_EXPANSION **不在此列**：它没有 worker/pane/outbox，只等协调者调 expand，无可轮询之物。"""
        return [t for t in self.tasks.values() if t.status in INFLIGHT]

    def expandable_tasks(self) -> list[Task]:
        """当前处于 AWAITING_EXPANSION（fan-out 模板 deps 已满足、等协调者展开）的节点。
        供 CLI/report/看板提示协调者「该读上游产出、决定 K、调 expand 了」。"""
        return [t for t in self.tasks.values() if t.status == AWAITING_EXPANSION]

    def expand_fanout(self, template_id: str, children_goals: list[str],
                      children_expects: list[str] | None = None) -> dict[str, Any]:
        """把 fan-out 模板节点展开成 len(children_goals) 个子任务，重接下游依赖，模板置 BYPASSED 容器。

        **只改内存、不校验/不落盘**——调用方（cli.cmd_expand）负责随后 validate()+refresh_ready()+save()。
        goal 的 {{shard}}/{{total}} 替换由调用方做好后经 children_goals 传入（board 不碰模板槽逻辑）。

        步骤：
          1) 生成子任务 <template_id>.1 .. <template_id>.K：clone 模板的 capabilities/needs/see_artifacts/
             context_refs/重试与超时整定，打上 fanout_group=模板id、fanout_max_parallel=模板并发上限。
             子任务**不继承** fanout（自己不是模板）与 reject_to（打回是评审节点职责，不随扇出复制）。
          2) 重接线：凡 needs/see_artifacts/reject_to 里引用模板 id 的下游，替换成全部子任务 id——
             这样 join 节点改为依赖真实子任务（展开前依赖模板，用于上游失败级联；展开后依赖子任务）。
          3) 模板置 BYPASSED（视同已满足的容器，不再派发、进终态判定），记录展开摘要。
        返回 {ok, error?, children:[...], rewired:[...]}。
        """
        t = self.tasks.get(template_id)
        if t is None:
            return {"ok": False, "error": f"节点 {template_id!r} 不存在", "children": [], "rewired": []}
        if not t.fanout:
            return {"ok": False, "error": f"节点 {template_id!r} 不是 fan-out 模板（无 fanout 声明）",
                    "children": [], "rewired": []}
        if t.status in TERMINAL:
            return {"ok": False, "error": f"节点 {template_id!r} 已是终态 {t.status}，无法展开（是否已展开过？）",
                    "children": [], "rewired": []}
        if not children_goals:
            return {"ok": False, "error": "children_goals 为空，至少展开 1 个子任务", "children": [], "rewired": []}
        if children_expects is not None and len(children_expects) != len(children_goals):
            # 与其它错误分支一致返回失败结果、而非抛 IndexError 冒泡打断整轮调度
            return {"ok": False,
                    "error": f"children_expects 长度({len(children_expects)})须与 children_goals({len(children_goals)})一致",
                    "children": [], "rewired": []}
        k = len(children_goals)
        child_ids = [f"{template_id}.{i}" for i in range(1, k + 1)]
        clash = [cid for cid in child_ids if cid in self.tasks]
        if clash:
            return {"ok": False, "error": f"子任务 id 已存在 {clash}（模板可能已展开过）",
                    "children": [], "rewired": []}
        max_parallel = int(t.fanout.get("max_parallel", 1))
        for idx, cid in enumerate(child_ids):
            self.tasks[cid] = Task(
                task_id=cid,
                goal=children_goals[idx],
                capabilities=list(t.capabilities),
                needs=list(t.needs),
                see_artifacts=list(t.see_artifacts),
                context_refs=list(t.context_refs),
                expect=(children_expects[idx] if children_expects else t.expect),
                max_retries=t.max_retries,
                timeout_s=t.timeout_s,
                max_lifetime_s=t.max_lifetime_s,
                awaiting_timeout_s=t.awaiting_timeout_s,
                fanout_group=template_id,
                fanout_max_parallel=max_parallel,
            )
        # 重接下游：把引用模板 id 的 needs/see_artifacts/reject_to 替换成全部子任务 id
        rewired: list[str] = []
        for other in self.tasks.values():
            if other.task_id == template_id or other.task_id in child_ids:
                continue
            touched = False
            for fld in ("needs", "see_artifacts", "reject_to"):
                lst = getattr(other, fld)
                if template_id in lst:
                    setattr(other, fld,
                            [x for x in lst if x != template_id] + list(child_ids))
                    touched = True
            if touched:
                rewired.append(other.task_id)
        t.status = BYPASSED
        t.result_summary = f"fan-out 展开为 {k} 个子任务：{child_ids}"
        t.error = None
        return {"ok": True, "children": child_ids, "rewired": rewired}

    def active_by_group(self) -> dict[str, int]:
        """各 fanout_group 当前仍持有 pane（在途）的子任务数——fan-out 组级并发闸的结算依据。"""
        counts: dict[str, int] = {}
        for t in self.tasks.values():
            if t.fanout_group and t.status in INFLIGHT:
                counts[t.fanout_group] = counts.get(t.fanout_group, 0) + 1
        return counts

    def done_deps(self, t: Task) -> list[str]:
        return [n for n in t.needs if self.tasks.get(n) and self.tasks[n].status == DONE]

    def inherited_artifacts(self, t: Task) -> list[dict[str, Any]]:
        """按 t.see_artifacts 现算「要给下游看的上游产物」：[{"from": <task_id>, "files": [...]}]。

        文件取上游节点写回的 artifacts（bootstrap 已要求 worker 写绝对路径），原样传递。
        只收在场且已 DONE 的上游（BYPASSED 无产物、未完成的取不到）——由 validate 保证
        see_artifacts ⊆ needs，正常分派时这些节点必已终态。上游无产物则 files 为空列表。"""
        out: list[dict[str, Any]] = []
        for n in t.see_artifacts:
            up = self.tasks.get(n)
            if up is None:
                continue
            out.append({"from": n, "files": list(up.artifacts or [])})
        return out

    def is_complete(self) -> bool:
        # SKIPPED/BYPASSED 与 DONE/FAILED 一样是终态：含跳过/绕过节点的 Job 也能干净收敛。
        return all(t.status in TERMINAL for t in self.tasks.values())

    def has_failures(self) -> bool:
        return any(t.status in FAILURE for t in self.tasks.values())

    def summary(self) -> str:
        from collections import Counter
        c = Counter(t.status for t in self.tasks.values())
        return " ".join(f"{k}={v}" for k, v in sorted(c.items()))
