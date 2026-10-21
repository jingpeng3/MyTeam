"""数据模型：清单、任务、DAG、信号。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

# 可命名的模板槽 `{{name}}`：只用于枚举/替换可填充的槽（cli 填槽、看板列模板所需槽共用）。
PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")
# 未填模板槽的**兜底**正则：只要还剩任何 `{{…}}`（无论槽名是否规范）就算残留。
# plan 漏填守卫与 retask 守卫共用这一条，杜绝「一处修了另一处仍漏」——兜底守卫必须比 PLACEHOLDER_RE 宽。
UNFILLED_SLOT_RE = re.compile(r"\{\{.*?\}\}")

# dry-run 分派写进 runtime 账本的占位 pane_id：没有真实 pane，回收/存活判定都要跳过它。
DRY_PANE = "<dry-run>"


def runtime_key(goal_id: str, task_id: str) -> str:
    """runtime.json 的复合键 `<goal_id>:<task_id>`（goal_id/task_id 已限定安全字符集、不含冒号）。"""
    return f"{goal_id}:{task_id}"


def split_runtime_key(key: str) -> tuple[str, str]:
    """runtime_key 的逆：返回 (goal_id, task_id)；畸形键（无冒号）task_id 为空串。"""
    gid, _, tid = key.partition(":")
    return gid, tid

# 全框架默认 runtime：新建 agent 模板、Runtime 缺省与 merge 回退都引用这里，避免三处漂移。
DEFAULT_CLI = "ducc"
DEFAULT_KIND = "claude"     # herdr agent kind


@dataclass
class Runtime:
    cli: str = DEFAULT_CLI
    kind: str = DEFAULT_KIND
    model: str = ""
    flags: list[str] = field(default_factory=list)

    @classmethod
    def merge(cls, base: dict, override: dict) -> Runtime:
        d = {**(base or {}), **{k: v for k, v in (override or {}).items() if v}}
        return cls(
            cli=d.get("cli", DEFAULT_CLI),
            kind=d.get("kind", DEFAULT_KIND),
            model=d.get("model", ""),
            flags=list(d.get("flags", [])),
        )


@dataclass
class AgentManifest:
    name: str
    role: str = ""
    description: str = ""
    prompt: str = "prompt.md"
    runtime: Runtime = field(default_factory=Runtime)
    workspace_path: str = "./workspace"
    worktree_base: str = ""
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # 该 agent 的 worker 要额外隐藏的 skill（并集到 defaults.worker_hidden_skills 之上，见
    # config.worker_settings_overrides）。区别于 skills：skills 是「告知」协调者/看板它有哪些
    # 能力，不强制隐藏；hidden_skills 是「强制」——分派时落进 worker cwd 的 settings.json。
    hidden_skills: list[str] = field(default_factory=list)
    lifecycle: str = "on-demand"
    max_concurrent: int = 1     # 该 agent 在**单个 Job 内**的并发 pane 上限（按 Job 独立结算，不跨 Job 共享）
    dir: str = ""               # 绝对路径，加载时填充

    def matches(self, needed: list[str]) -> int:
        """返回与所需能力的匹配数（用于打分）。"""
        return len(set(self.capabilities) & set(needed or []))


# 任务状态机
# SKIPPED：上游 FAILED/SKIPPED 导致本节点永不可达，级联标记的**失败**终态（区别于"还没轮到"的 pending）。
# BYPASSED：协调者读上游摘要后**主动判定无需执行**、绕过的终态。语义与 SKIPPED 相反：
#   它「视同已满足」——deps_done 把它当 DONE 计，下游据此转 ready 照常执行；且**不计入失败**
#   （不进 has_failures、不触发级联跳过）。区别于「上游坏了连累不可达」的 SKIPPED。
# AWAITING_INPUT：worker 已 dispatched 但**停下来等人拍板**（非挂死、非失败）。存活、非终态：
#   is_complete/has_failures 都不含它（Job 保持未完成、不算失败），非 DONE 故 deps_done 不放行下游；
#   人答复后 monitor 把它恢复为 dispatched 继续跑。区别于「等待输入被当挂死回收」的旧行为。
# AWAITING_EXPANSION：**fan-out 模板节点**的依赖已满足、等协调者按上游产出**动态展开**成 K 个子任务
#   （见 Task.fanout）。存活、非终态、无 worker（从不直接派发、无 pane/outbox）：is_complete/has_failures
#   都不含它、非 DONE 故不放行下游；协调者读上游 outbox 决定 K 后调 `expand` 把它展开、置为 BYPASSED 容器，
#   子任务转 ready 照常执行。区别于 AWAITING_INPUT（那是已派发的 worker 停下等人、有 pane）。
PENDING, READY, DISPATCHED, DONE, FAILED, SKIPPED, BYPASSED, AWAITING_INPUT, \
    AWAITING_EXPANSION = (
        "pending", "ready", "dispatched", "done", "failed", "skipped", "bypassed",
        "awaiting_input", "awaiting_expansion",
    )
STATUSES = frozenset({PENDING, READY, DISPATCHED, DONE, FAILED, SKIPPED,
                      BYPASSED, AWAITING_INPUT, AWAITING_EXPANSION})
# 状态分组（判定口径集中一处，避免各模块手写元组漂移）：
TERMINAL = frozenset({DONE, FAILED, SKIPPED, BYPASSED})   # 终态：Job 全为终态即收敛
INFLIGHT = frozenset({DISPATCHED, AWAITING_INPUT})        # 在途：持有 pane/账本条目，回收时要一并清理
FAILURE = frozenset({FAILED, SKIPPED})                    # 失败：连累下游级联跳过、计入 has_failures
SATISFIED = frozenset({DONE, BYPASSED})                   # 依赖已满足：下游可转 ready

# REJECT 是 worker outbox 的**结果动词**（评审/校验节点判定上游产出有问题、打回），
# 不是持久任务状态，故**不入 STATUSES**。协调者读到它：把打回目标 + 其全部后代重置为
# PENDING 重跑，并把打回意见与证据作为「凭证」累积注入目标，防止重复错误执行。
REJECT = "reject"


@dataclass
class Task:
    task_id: str
    goal: str
    capabilities: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)        # 依赖的 task_id
    agent: str = ""              # 分派到的 agent name
    tried_agents: list[str] = field(default_factory=list)  # 已试过的 agent（重试时优先换人绕开）
    context_refs: list[str] = field(default_factory=list)
    # 选择性继承上游产物：列出「要看其产出文件」的上游 task_id（必须 ⊆ needs）。
    # 分派时框架把这些节点写回的 artifacts 绝对路径注入信号 inherited_artifacts，下游按需读取。
    # 空=不自动继承任何上游产物（默认，保持旧行为）；与 needs 解耦——依赖它未必要看它的产物。
    see_artifacts: list[str] = field(default_factory=list)
    # 打回（reject/send-back）声明：本节点作为评审/校验节点时，允许「打回」的上游 task_id 列表。
    # 空=不能打回（默认）。载入期 Board.validate 校验必须 ⊆ 本节点的**传递上游（祖先闭包）**——
    # 只能打回你消费过其产出的上游。reject_to 不进 needs、是运行期回退边，不引入前向环。
    reject_to: list[str] = field(default_factory=list)
    # 本节点作为**打回目标**时的累计被打回上限；0=用全局 MAX_REJECTS。防「实现↔评审」无限乒乓。
    max_rejects: int = 0
    expect: str = "改完后把结果写 outbox"
    status: str = PENDING
    attempts: int = 0            # 业务尝试数：worker 真正接单执行才 +1（provisioning 抖动不计）
    provision_attempts: int = 0  # 基础设施尝试数：herdr 拉起失败重试计数，独立于 max_retries
    max_retries: int = 1
    timeout_s: int = 0           # 单任务分派后无 outbox 的「交付超时」(秒)；0=用全局默认
    max_lifetime_s: int = 0      # 分派后的绝对存活上限(秒)；0=按 factor×交付超时派生。与交付超时解耦，
                                 # 让「快检静默死亡(小 timeout_s)」与「容忍合法长任务(大 max_lifetime_s)」可独立整定
    awaiting_timeout_s: int = 0  # 进入 AWAITING_INPUT（等人拍板）后的存活上限(秒)；0=用全局 AWAITING_INPUT_TIMEOUT。
                                 # 等人期间暂停 max_lifetime_s 硬上限，仅受本上限约束，超时才判失败回收。
    # 完成后回填的结果快照（供最终报告；outbox 信号会被清理）
    result_summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    # 交付哨兵：DONE 时 outbox 报了、但落盘查无此文件的**绝对**产物路径。只警告不判失败
    # （存在性只是启发式，详见 coordinator._missing_artifacts），由协调者按 skip/retask/reject 处置。
    artifact_warnings: list[str] = field(default_factory=list)
    error: str | None = None
    # 被下游打回累积的「凭证」：每条 {from, reason, evidence:[abs...], ts}。作为打回目标重跑时，
    # 由框架注入信号(rejections)与引导语，让 worker 看到历次打回意见+证据、针对性修复、勿重复犯错。
    # 永不清空（历史全留），据此防重复错误执行。
    rejections: list[dict] = field(default_factory=list)
    reject_count: int = 0        # 已被打回次数（loop guard 计数：达 max_rejects 上限即升级给协调者，不再自动回退）
    # ---- 动态并行（fan-out）----
    # 本节点是 fan-out **模板**：不直接派发，deps 满足后转 AWAITING_EXPANSION，等协调者按上游产出调
    # `expand` 动态展开成 K 个子任务。dict：{"max_parallel": 并发上限, "shard_var": 分片号槽名(默认 shard),
    # "total_var": 总数槽名(默认 total)}。展开时子任务 goal 里 {{shard_var}}→i、{{total_var}}→K 被替换。
    # None（默认）=普通节点，行为一字不变。
    fanout: dict | None = None
    # 本节点是 expand 生成的**子任务**：同组共享一个并发闸。空=非 fan-out 子任务（默认）。
    fanout_group: str = ""       # = 模板 task_id，调度按 (goal_id, fanout_group) 结算在途数
    fanout_max_parallel: int = 0  # 从模板 fanout.max_parallel 拷来：同组同时最多几个在跑，其余排队等容量
    # ---- 执行计时 ----
    # runtime.json 的 dispatched_at 收尾即删、且等人恢复时被刻意重置，不能用来算耗时；计时单独落在 board。
    ready_at: float | None = None   # 最近一次 PENDING→READY 的时刻，派发时据此算排队等容量耗时
    # 每次派发一条运行记录（见 open_run），只追加不清空——重试/打回/重置的返工耗时都留痕。
    runs: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        """构造即校验：非法状态/负数配置在此挡下，不拖到调度期变成模糊的 stuck。"""
        if self.status not in STATUSES:
            raise ValueError(
                f"task {self.task_id!r} 状态非法: {self.status!r}（合法：{sorted(STATUSES)}）")
        for fld in ("attempts", "provision_attempts", "max_retries",
                    "timeout_s", "max_lifetime_s", "awaiting_timeout_s",
                    "max_rejects", "reject_count", "fanout_max_parallel"):
            v = getattr(self, fld)
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"task {self.task_id!r} 字段 {fld} 必须是非负整数，得到 {v!r}")
        if self.fanout is not None:
            if not isinstance(self.fanout, dict):
                raise ValueError(f"task {self.task_id!r} 的 fanout 必须是对象，得到 {type(self.fanout).__name__}")
            mp = self.fanout.get("max_parallel", 0)
            if not isinstance(mp, int) or mp < 1:
                raise ValueError(
                    f"task {self.task_id!r} 的 fanout.max_parallel 必须是 >=1 的整数，得到 {mp!r}")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        """从磁盘/信号安全反序列化：只取已知字段，多出的旧字段（如历史 runtime=None）被忽略而非抛错。"""
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ---------- 执行计时 ----------
    # 一条运行记录：{attempt, agent, started_at, ended_at, end_source, queue_s, provision_s,
    #   awaiting_s, awaiting_since, wall_s, active_s, outcome}。
    # wall_s = ended_at - started_at；active_s = wall_s - awaiting_s（等人拍板不算执行）。
    # outcome：done / failed / timeout / fatal / reject / reject_invalid / bypassed / retasked / reset。
    def current_run(self) -> dict | None:
        """未收尾的最近一条运行记录；无则 None。"""
        if self.runs and self.runs[-1].get("ended_at") is None:
            return self.runs[-1]
        return None

    def open_run(self, agent: str, now: float, provision_s: float | None = None) -> dict:
        """派发提交时开一条运行记录。残留的未收尾记录先按 superseded 收掉，保证同时只有一条在开。"""
        self.close_run("superseded", now)
        run = {"attempt": self.attempts, "agent": agent,
               "started_at": now, "ended_at": None, "end_source": "",
               "queue_s": round(max(0.0, now - self.ready_at), 3) if self.ready_at else None,
               "provision_s": round(provision_s, 3) if provision_s is not None else None,
               "awaiting_s": 0.0, "awaiting_since": None,
               "wall_s": None, "active_s": None, "outcome": ""}
        self.runs.append(run)
        return run

    def mark_awaiting(self, now: float) -> None:
        """进入等人拍板：记锚点（已在等则不动）。"""
        run = self.current_run()
        if run is not None and run.get("awaiting_since") is None:
            run["awaiting_since"] = now

    def mark_resumed(self, now: float) -> None:
        """等人结束（人已答复 / worker 恢复）：把本段等待累计进 awaiting_s。"""
        run = self.current_run()
        if run is not None and run.get("awaiting_since") is not None:
            run["awaiting_s"] = round(run["awaiting_s"] + max(0.0, now - run["awaiting_since"]), 3)
            run["awaiting_since"] = None

    def close_run(self, outcome: str, now: float, ended_at: float | None = None) -> dict | None:
        """收尾当前运行记录；无在开记录则 no-op 返回 None。

        ended_at 给的是 worker 写 outbox 的时刻（比 monitor 观测到早，逐轮驱动时可差几分钟），
        夹到 [started_at, now] 内防时钟异常；不给则按观测时刻 now 记。"""
        run = self.current_run()
        if run is None:
            return None
        start = run["started_at"]
        if ended_at is not None:
            end, run["end_source"] = min(max(ended_at, start), now), "outbox_mtime"
        else:
            end, run["end_source"] = now, "observed"
        self.mark_resumed(end)
        run["ended_at"] = end
        run["wall_s"] = round(end - start, 3)
        run["active_s"] = round(max(0.0, run["wall_s"] - run["awaiting_s"]), 3)
        run["outcome"] = outcome
        return run

    @staticmethod
    def run_live(run: dict, now: float) -> dict:
        """运行记录的「当前值」视图：在开的记录按 now 现算 wall/active/awaiting，不改原记录。"""
        if run.get("ended_at") is not None:
            return run
        aw = run.get("awaiting_s") or 0.0
        if run.get("awaiting_since") is not None:
            aw += max(0.0, now - run["awaiting_since"])
        wall = max(0.0, now - run["started_at"])
        return {**run, "awaiting_s": round(aw, 3), "wall_s": round(wall, 3),
                "active_s": round(max(0.0, wall - aw), 3)}

    def timing(self, now: float) -> dict[str, Any]:
        """本任务计时摘要：最近一次运行的排队/墙钟/净执行/等人 + 全部运行的累计净执行。"""
        live = [self.run_live(r, now) for r in self.runs]
        last = live[-1] if live else {}
        return {
            "runs": len(live),
            "running": bool(live) and self.runs[-1].get("ended_at") is None,
            "started_at": last.get("started_at"),
            "ended_at": last.get("ended_at"),
            "queue_s": last.get("queue_s"),
            "provision_s": last.get("provision_s"),
            "wall_s": last.get("wall_s"),
            "active_s": last.get("active_s"),
            "awaiting_s": last.get("awaiting_s"),
            "outcome": last.get("outcome", ""),
            "total_active_s": round(sum(r.get("active_s") or 0.0 for r in live), 3),
        }

    def to_signal(self, deps_done: list[str],
                  inherited: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "context_refs": self.context_refs,
            "deps_done": deps_done,
            # 选中上游节点的产物文件：[{"from": <task_id>, "files": [<abs path>...]}]，
            # 分派时由 board 按 see_artifacts 查上游 artifacts 现算注入；空=未声明或上游无产物。
            "inherited_artifacts": inherited or [],
            # 若本节点是评审/校验节点、声明了可打回的上游，透传给 worker 供其判定后写 reject 信号。
            "reject_to": list(self.reject_to),
            # 本节点作为打回目标重跑时的「凭证」：历次下游打回意见+证据（绝对路径），
            # worker 必读、针对性修复、勿重复同样问题。空=从未被打回。
            "rejections": list(self.rejections),
            "expect": self.expect,
        }


@dataclass
class Result:
    task_id: str
    status: str
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    retryable: bool | None = None   # worker 显式声明失败可否重试；None=未声明(按 max_retries)
    reject_to: str = ""                # status=REJECT 时：打回的目标上游 task_id（其余状态忽略）
    written_at: float | None = None  # outbox 文件 mtime＝worker 实际交付时刻（计时用；读不到为 None）
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
