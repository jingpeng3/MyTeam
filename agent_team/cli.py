"""agent-team 命令行入口：agent 维护 + 协调调度。"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, NoReturn

from . import coordinator, registry, retro
from .board import Board, DagError, job_lock
from .config import Paths, load_manifest, read_json_or
from .models import Task, PLACEHOLDER_RE, UNFILLED_SLOT_RE


def _setup_logging(level: str | None) -> None:
    """库层用 getLogger(__name__) 记事件，这里统一配 handler。

    日志一律走 stderr，绝不污染命令的 JSON stdout 输出；级别由 --log-level 或
    环境变量 TEAMS_LOG 控制，默认 WARNING（只在异常/并发/隔离等值得关注时出声）。
    """
    lvl = (level or os.environ.get("TEAMS_LOG") or "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _paths() -> Paths:
    return Paths()


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _die(msg: str) -> NoReturn:
    """非法输入：一行 stderr + 退出码 2，不吐 traceback。"""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


# goal_id / task_id 会进文件名、信号命名空间、runtime 复合键与看板标识：限定安全字符集
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


# ---------------- agent 维护 ----------------
def cmd_agent_create(a) -> int:
    p = _paths()
    if a.max_concurrent < 1:          # <1 会让 free=max_concurrent-used 恒≤0，agent 悄悄永不接单
        _die(f"agent create 非法：--max-concurrent 需 >= 1，得到 {a.max_concurrent}")
    caps = [c.strip() for c in a.capabilities.split(",") if c.strip()] if a.capabilities else []
    try:
        d = registry.create_agent(p, a.name, a.role or "", caps,
                                  max_concurrent=a.max_concurrent)
    except FileExistsError as e:      # 重名：一行 stderr + 干净退出，不吐 traceback
        _die(f"agent create 失败：{e}")
    print(f"created {d}")
    return 0


def cmd_agent_list(a) -> int:
    p = _paths()
    rows = [{"name": m.name, "role": m.role, "capabilities": m.capabilities,
             "lifecycle": m.lifecycle, "cli": m.runtime.cli} for m in registry.list_agents(p)]
    _print(rows)
    return 0


def cmd_agent_show(a) -> int:
    try:
        m = load_manifest(_paths(), a.name)
    except FileNotFoundError:
        _die(f"agent show 失败：未找到 agent {a.name}（用 `agent list` 查看）")
    _print({"name": m.name, "role": m.role, "prompt": m.prompt,
            "workspace": m.workspace_path, "capabilities": m.capabilities,
            "skills": m.skills, "hidden_skills": m.hidden_skills, "lifecycle": m.lifecycle,
            "runtime": vars(m.runtime)})
    return 0


def cmd_agent_remove(a) -> int:
    p = _paths()
    if not registry.remove_agent(p, a.name, purge=a.purge):
        print(f"agent remove：未找到 agent {a.name}，未做改动", file=sys.stderr)
        return 1
    print(f"removed {a.name}" + (" (purged)" if a.purge else " (kept dir)"))
    return 0


# ---------------- 协调调度（多 Job） ----------------
def _locks_job(attr: str):
    """CLI 介入命令（skip/retask/answer/reject/expand/job remove…）整段持该 Job 的驱动锁：
    「载入 → 改 → 落盘」期间后台 watch / 别的驱动者不会插进来收割，互不覆盖。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(a) -> int:
            gid = getattr(a, attr, "") or ""
            if not gid:
                return fn(a)
            with job_lock(_paths(), gid):
                return fn(a)
        return wrapper
    return deco


def _require_board(p: Paths, goal_id: str) -> Board:
    b = Board.load(p, goal_id)
    if b is None:
        _die(f"未找到 Job: {goal_id}（用 `jobs` 查看）")
    return b


def _task_from_raw(t: dict, idx: int) -> Task:
    """把 plan 里的一个 task dict 归一化为 Task：别名映射 + 必填校验 + 未知字段告警。

    修复字段漂移坑：文档/引导语常写 instruction/title，而 Task 用 goal；旧实现静默丢弃
    未知键、缺 goal 时抛裸 TypeError。这里统一别名、缺必填给友好报错、未知键告警不静默。
    """
    t = dict(t)
    if "goal" not in t:                  # instruction/title/description 作为 goal 的别名
        for alias in ("instruction", "title", "description"):
            if t.get(alias):
                t["goal"] = t[alias]
                break
    for req in ("task_id", "goal"):
        if not t.get(req):
            ident = t.get("task_id", f"#{idx}")
            _die(f"plan 非法：task {ident} 缺少必填字段 `{req}`")
    known = set(Task.__dataclass_fields__)
    unknown = [k for k in t if k not in known and k not in
               ("instruction", "title", "description")]
    if unknown:
        print(f"warning: task {t['task_id']} 忽略未知字段 {unknown}", file=sys.stderr)
    return Task.from_dict(t)   # 只取已知字段构造（未知键上面已告警）


def _walk_strings(obj, fn):
    """递归对结构里所有字符串应用 fn，返回同构新对象（键名不动，只处理值/元素）。"""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk_strings(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk_strings(v, fn) for k, v in obj.items()}
    return obj


def _collect_placeholders(obj) -> set[str]:
    found: set[str] = set()
    _walk_strings(obj, lambda s: (found.update(PLACEHOLDER_RE.findall(s)), s)[1])
    return found


def _collect_leftover(obj) -> list[str]:
    """漏填**兜底**扫描：收集结构里所有 `{{…}}` 原始 token（无论槽名是否规范）。
    `PLACEHOLDER_RE` 只认可命名的规范槽 `{{ok}}`，`{{a:b}}`/`{{}}`/`{{a b}}` 这类明显是模板残留却
    含非 `[\\w.\\-]` 字符的 token 会从它眼皮下溜走——既不列为槽、又不被替换、又不被判残留，直接
    穿透给 worker。这里用与 retask 一致的宽正则兜底，兑现「绝不把半成品 goal 发给 worker」。"""
    found: list[str] = []
    _walk_strings(obj, lambda s: (found.extend(UNFILLED_SLOT_RE.findall(s)), s)[1])
    return found


def _slot_inner_name(tok: str) -> str:
    """从漏填兜底 token（形如 `{{name}}`）取内层槽名；非规范 token 原样返回（不会被豁免）。"""
    if tok.startswith("{{") and tok.endswith("}}"):
        return tok[2:-2].strip()
    return tok


def _fanout_slot_names(data: dict) -> set[str]:
    """收集 plan 里所有 fan-out 模板节点的分片号槽名（shard_var/total_var，默认 shard/total）。
    这些槽是运行期 expand 才填的，plan 载入的待填/漏填检查须豁免它们。"""
    out: set[str] = set()
    for t in data.get("tasks", []) if isinstance(data.get("tasks"), list) else []:
        fo = t.get("fanout") if isinstance(t, dict) else None
        if isinstance(fo, dict):
            out.add(fo.get("shard_var", "shard"))
            out.add(fo.get("total_var", "total"))
    return out


def _substitute(obj, values: dict[str, str]):
    return _walk_strings(
        obj, lambda s: PLACEHOLDER_RE.sub(
            lambda m: values.get(m.group(1), m.group(0)), s))


def _load_json(path: str, what: str) -> Any:
    """读 JSON 文件：缺失/不可读/非 UTF-8/JSON 截断等一律 `_die` 一行报错，不吐 traceback。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:                 # 不存在 / 是目录 / 无权限
        _die(f"{what} 读取失败：{path}（{e.strerror or e}）")
    except ValueError as e:              # JSONDecodeError / UnicodeDecodeError 都是 ValueError 子类
        _die(f"{what} 不是合法 JSON：{path}（{e}）")


def _parse_vars(a) -> dict[str, str]:
    """合并 --vars-file(JSON 对象) 与 --var key=value；--var 优先，便于命令行临时覆写。"""
    values: dict[str, str] = {}
    if a.vars_file:
        loaded = _load_json(a.vars_file, "--vars-file")
        if not isinstance(loaded, dict):
            _die(f"--vars-file 必须是 JSON 对象(key->value)，得到 {type(loaded).__name__}")
        values.update({str(k): str(v) for k, v in loaded.items()})
    for kv in a.var or []:
        k, sep, v = kv.partition("=")
        if not sep:
            _die(f"--var 需为 key=value 形式，得到 {kv!r}")
        values[k.strip()] = v
    return values


def cmd_plan(a) -> int:
    p = _paths()
    data = _load_json(a.file, "plan 文件")
    if not isinstance(data, dict):
        _die(f"plan 非法：{a.file} 顶层必须是 JSON 对象(plan)，得到 {type(data).__name__}——"
             f"vars/数据文件不是 plan，请用 --vars-file 传入、并放到 coordinator/plans/vars/（勿放 plans/ 根）")
    slots = _collect_placeholders(data)
    # fan-out 模板的分片号槽（shard_var/total_var）是**运行期** expand 才填的，不是 plan 载入槽：
    # 从待填槽与漏填兜底里豁免它们，否则模板 goal 里的 {{shard}}/{{total}} 会被误判为漏填而拒载。
    fanout_vars = _fanout_slot_names(data)
    slots = {s for s in slots if s not in fanout_vars}
    if a.show_vars:                          # 只introspect模板需要哪些槽，不落地 Job
        _print({"template": data.get("goal_id"), "goal": data.get("goal", ""),
                "objective": data.get("objective", ""), "placeholders": sorted(slots)})
        return 0
    # 载入前先确认这确实是个 plan（含非空 tasks 列表）。放错位置的数据文件（如 --vars-file 的
    # key→value 值文件，无 tasks/无 goal_id）在此被清晰拒绝，而不是后面撞 KeyError('goal_id')。
    if not isinstance(data.get("tasks"), list) or not data["tasks"]:
        _die(f"plan 非法：{a.file} 缺少非空 tasks 列表——它看起来不是 plan。"
             f"若这是 --vars-file 的值文件/数据文件，请放到 coordinator/plans/vars/ 并用 --vars-file 传入，别当 plan 载入")
    # plan 文件是可复用「模板」：goal 里可留 {{槽}}，协调者按用户目标填充各阶段目标。
    values = _parse_vars(a)
    data = _substitute(data, values)
    # --as 覆写实例 id（先于漏填检查，模板 goal_id 本身若是 {{槽}} 也被实例名顶掉）。
    gid = a.as_goal or data.get("goal_id")
    if not isinstance(gid, str) or not gid:  # 缺失/空/非字符串（如数字）都拒，免得后面撞 KeyError/TypeError
        _die(f"plan 非法：{a.file} 缺少非空字符串 goal_id（得到 {gid!r}），请在 plan 里填写或用 --as 指定")
    if a.as_goal:
        data["goal_id"] = gid
    leftover = sorted(set(_collect_leftover(data)))
    # 豁免 fan-out 分片号槽：模板 goal 里的 {{shard}}/{{total}} 是运行期 expand 才替换的，不算漏填。
    leftover = [tok for tok in leftover if _slot_inner_name(tok) not in fanout_vars]
    if leftover:                             # 半成品 goal 绝不发给 worker（否则又是一次"假失败"级坑）
        _die(f"plan 未填充：仍有占位槽 {leftover}——请用 --var/--vars-file 补齐"
             f"（`plan {a.file} --show-vars` 可列出规范槽；含 `:`/空格/`/` 等特殊字符的残留 token 需改模板）")
    # goal_id 是「实例」标识：--as 覆写后，同一模板可并发起多个互不干扰的 Job
    # （信号/产物/runtime/看板本就全按 goal_id 隔离）。不带 --as 时沿用文件内 goal_id。
    if not _SAFE_ID_RE.fullmatch(gid):
        _die(f"plan 非法：goal_id {gid!r} 只能含字母/数字/.、_、-（会用作文件名与信号命名空间）")
    prev = Board.load(p, gid)
    raw = data["tasks"]
    bad_items = [i for i, t in enumerate(raw) if not isinstance(t, dict)]
    if bad_items:                            # 先于取 task_id：非对象项会让 t.get 抛 AttributeError
        _die(f"plan 非法：tasks 每项须为 JSON 对象，第 {bad_items} 项不是（得到 {raw[bad_items[0]]!r}）")
    ids = [t.get("task_id") for t in raw]
    dupes = sorted({i for i in ids if i and ids.count(i) > 1})
    if dupes:
        _die(f"plan 非法：重复 task_id {dupes}")
    # task_id 会进信号文件名、runtime 复合键、看板 onclick 等上下文：限定安全字符集（与 goal_id 一致，
    # 也正好落地 CLAUDE.md「task_id 用英文短横线命名」的约定）。放开会招来引号断链/文件名注入等隐患。
    bad_ids = sorted(i for i in ids if i and not _SAFE_ID_RE.fullmatch(i))
    if bad_ids:
        _die(f"plan 非法：task_id {bad_ids} 只能含字母/数字/.、_、-（用作文件名/信号命名空间/看板标识）")
    try:
        tasks = [_task_from_raw(t, i) for i, t in enumerate(raw)]
    except ValueError as e:              # __post_init__ 校验：非法状态/负数配置
        _die(f"plan 非法：{e}")
    if not isinstance(data.get("retro", False), bool):
        _die(f"plan 非法：顶层 retro 只能是 true/false（流程级复盘开关），得到 {data['retro']!r}")
    board = Board(gid, data.get("goal", ""), tasks)
    board.retro = data.get("retro", False)   # 流程级复盘开关随模板走；还需 team.yaml retro.enabled 才生效
    try:
        board.validate()                 # 悬空依赖 / 成环，载入期即报错
    except DagError as e:
        _die(f"plan 非法：{e}")
    with job_lock(p, gid):               # 替换同名 Job 时别和后台 watch 的收割交错
        _purge_goal_signals(p, gid)      # 清掉同名 Job 的陈旧信号+在途 pane，避免"假完成"/孤儿
        board.refresh_ready()
        board.save(p)
    # 无 capabilities 的任务会被「谁都能干」路由到负载最空的**任意** agent（可能是不相干的生产 agent）。
    # 非致命但易误路由——载入期给一条告警，让协调者显式确认或补 capabilities。
    nocap = [t.task_id for t in tasks if not t.capabilities]
    if nocap:
        print(f"warning: 任务 {nocap} 未声明 capabilities，将被路由到任意负载最空的 agent"
              f"（可能误派给不相干 agent）——如需定向请补 capabilities", file=sys.stderr)
    note = ""
    if prev is not None and not prev.is_complete():
        note = "（覆盖了一个未完成的同名 Job）"
    src = f"（模板实例化：{gid}，填了 {len(values)} 个槽）" if (slots or values) else ""
    print(f"loaded job {gid}: {board.summary()} {src}{note}")
    return 0


def _clear_goal_signal_files(p: Paths, goal_id: str) -> None:
    """删除某 Job 在所有 agent inbox/outbox 下的信号文件（<goal_id>__*.json）。
    plan 重载前清残留、job remove --purge 时清理，两处共用同一段 glob。"""
    if not p.agents_dir.exists():
        return
    for adir in p.agents_dir.iterdir():
        for box in ("inbox", "outbox"):
            d = adir / box
            if d.exists():
                for f in d.glob(f"{goal_id}__*.json"):
                    f.unlink()


def _purge_goal_signals(p: Paths, goal_id: str) -> None:
    """载入 plan 前清理该 goal 残留的 in/outbox 信号、在途 pane 与 runtime 条目。
    runtime 条目由 close_goal_panes 在锁内一并剔除，这里不再无锁重写账本。"""
    coordinator.close_goal_panes(p, goal_id)   # 先回收在途 pane 再清账本，防孤儿
    _clear_goal_signal_files(p, goal_id)


def cmd_jobs(a) -> int:
    p = _paths()
    _print([{"goal_id": b.goal_id, "goal": b.goal, "summary": b.summary(),
             "complete": b.is_complete(), "retro": b.retro} for b in Board.load_all(p)])
    return 0


def cmd_status(a) -> int:
    p = _paths()
    if a.goal_id:
        b = _require_board(p, a.goal_id)
        _print({"goal_id": b.goal_id, "goal": b.goal, "summary": b.summary(),
                "complete": b.is_complete(),
                "awaiting": coordinator.awaiting_tasks(p, b),
                "tasks": [{"task_id": t.task_id, "status": t.status,
                           "agent": t.agent, "needs": t.needs,
                           "attempts": t.attempts} for t in b.tasks.values()]})
    else:
        _print([{"goal_id": b.goal_id, "summary": b.summary(),
                 "complete": b.is_complete()} for b in Board.load_all(p)])
    return 0


def _warn_all_jobs_scope(p: Paths, dry_run: bool) -> None:
    """不带 --job 的 tick/monitor 会作用于**所有**活跃 Job（可能含别的会话拥有的）。dry-run 尤甚：
    它仍会把别 Job 的 ready 任务写成 dispatched、落 inbox/runtime 幽灵，逼对方 monitor 多跑一轮。
    多于一个活跃 Job 时给一条告警，提醒用 --job 限定。"""
    active = [b for b in Board.load_all(p) if not b.is_complete()]
    if len(active) > 1:
        ids = ", ".join(b.goal_id for b in active)
        kind = "dry-run 预派" if dry_run else "调度"
        print(f"warning: 未加 --job，将对 {len(active)} 个活跃 Job 全部{kind}（{ids}）——"
              f"如只想动其中一个请加 --job <goal_id>", file=sys.stderr)


def cmd_tick(a) -> int:
    p = _paths()
    hold = not a.no_hold        # 交互 tick 默认 hold：本轮收割才解锁的下游留给协调者先审再派
    if a.job:
        res = coordinator.tick(p, _require_board(p, a.job), dry_run=a.dry_run, hold_new_ready=hold)
    else:
        _warn_all_jobs_scope(p, a.dry_run)
        res = coordinator.tick_all(p, dry_run=a.dry_run, hold_new_ready=hold)
    _print(res)
    return 0


def _monitor_view(p: Paths, goal_id: str, dry_run: bool) -> dict:
    """对单个 Job 跑一次 monitor（锁内重新载入，不和后台 watch 互相覆盖），返回给协调者看的结果视图。"""
    with job_lock(p, goal_id):
        b = _require_board(p, goal_id)
        changed = coordinator.monitor_once(p, b, close_panes=not dry_run)
        return {"changed": changed, **coordinator.job_view(p, b)}


def cmd_watch(a) -> int:
    from . import watch
    _print(watch.run(_paths(), poll=a.poll, idle_exit=a.idle_exit))
    return 0


def cmd_wait(a) -> int:
    p = _paths()
    if a.job:
        _require_board(p, a.job)
    _print(coordinator.wait_for_change(p, goal_id=a.job or None, timeout=a.timeout,
                                       poll=a.poll, since=a.since))
    return 0


def cmd_monitor(a) -> int:
    p = _paths()
    if a.job:
        out = _monitor_view(p, a.job, a.dry_run)
    else:
        _warn_all_jobs_scope(p, a.dry_run)
        out = {b.goal_id: _monitor_view(p, b.goal_id, a.dry_run)
               for b in Board.load_all(p) if not b.is_complete()}
    # Job 多在 monitor 收 outbox 时收敛：同一次调用就把复盘 Job 建好、把已出的建议收进待办池。
    # 只建不派——驱动方看到 retro_spawned 后 `tick --job <复盘id>` 派出并接着 monitor 到收敛。
    rr = coordinator.retro_round(p, a.dry_run)
    if rr["retro_spawned"] or rr["insights_new"]:
        out["retro"] = rr
    _print(out)
    return 0


def cmd_run(a) -> int:
    p = _paths()
    if a.job:
        _require_board(p, a.job)     # 提前校验 Job 存在，给友好报错
    _print(coordinator.run_loop(p, interval=a.interval, max_ticks=a.max_ticks,
                                dry_run=a.dry_run, goal_id=a.job or None))
    return 0


def _fmt_dur(sec) -> str:
    """秒数 → 人读时长（12s / 3m12s / 1h02m）；None → —。"""
    if sec is None:
        return "—"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def _table(rows: list[list[str]]) -> str:
    """等宽对齐的纯文本表（按显示宽度对齐：中文占 2 列）。"""
    import unicodedata

    def w(x: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in x)
    widths = [max(w(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c + " " * (widths[i] - w(c)) for i, c in enumerate(r)).rstrip()
                     for r in rows)


def _timing_text(rep: dict) -> str:
    """单 Job report → 计时文本：Job 墙钟 + 任务表 + agent 聚合表。逐轮汇报时直接贴。"""
    jt = rep.get("timing") or {}
    head = (f"Job {rep['goal_id']}  墙钟 {_fmt_dur(jt.get('wall_s'))}"
            f"{'' if rep.get('complete') else '（未完成）'}"
            f"  任务净执行合计 {_fmt_dur(jt.get('total_active_s'))}  [{rep.get('summary', '')}]")
    rows = [["task", "agent", "status", "排队", "拉起", "净执行", "等人", "尝试", "累计净执行"]]
    for t in rep["tasks"]:
        tm = t.get("timing") or {}
        active = _fmt_dur(tm.get("active_s")) + ("…" if tm.get("running") else "")
        rows.append([t["task_id"], t["agent"] or "—", t["status"],
                     _fmt_dur(tm.get("queue_s")), _fmt_dur(tm.get("provision_s")), active,
                     _fmt_dur(tm.get("awaiting_s")), str(tm.get("runs", 0)),
                     _fmt_dur(tm.get("total_active_s") if tm.get("runs") else None)])
    out = [head, "", _table(rows)]
    agents = rep.get("agent_timing") or []
    if agents:
        arows = [["agent", "次数", "成功", "失败", "在跑", "净执行合计", "平均", "最长", "等人合计"]]
        for a in agents:
            arows.append([a["agent"], str(a["runs"]), str(a["done"]), str(a["failed"]),
                          str(a["running"]), _fmt_dur(a["total_active_s"]),
                          _fmt_dur(a["avg_active_s"]), _fmt_dur(a["max_active_s"]),
                          _fmt_dur(a["total_awaiting_s"])])
        out += ["", _table(arows)]
    return "\n".join(out)


def cmd_report(a) -> int:
    rep = coordinator.report(_paths(), a.goal_id)
    if not a.timing:
        _print(rep)
        return 0
    if rep.get("error"):
        print(rep["error"], file=sys.stderr)
        return 1
    reps = rep["jobs"] if "jobs" in rep else [rep]
    print("\n\n".join(_timing_text(r) for r in reps) or "（无 Job）")
    return 0


def _plan_file(p: Paths, ref: str) -> Path:
    """plan 引用 → 文件：可给路径，也可给 coordinator/plans/ 下的模板名（省略 .json）。"""
    f = Path(ref)
    if not f.is_file():
        f = p.plans_dir / (ref if ref.endswith(".json") else f"{ref}.json")
    if not f.is_file():
        _die(f"未找到 plan：{ref}")
    return f


def _retro_view(p: Paths) -> dict:
    plans = {}
    for f in sorted(p.plans_dir.glob("*.json")):
        data = read_json_or(f, {})
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            plans[f.stem] = data.get("retro") is True
    return {"enabled": retro.globally_enabled(p), "has_retro_agent": retro.has_retro_agent(p),
            "plans": plans,
            "jobs": {b.goal_id: b.retro for b in Board.load_all(p) if not retro.is_retro(b.goal_id)}}


def cmd_retro_status(a) -> int:
    _print(_retro_view(_paths()))
    return 0


@_locks_job("job")
def cmd_retro_toggle(a) -> int:
    """on/off：不带目标=全局总闸；带 plan=模板的流程开关（之后载入的 Job 生效）；--job=已有 Job 的开关。"""
    p = _paths()
    on = a.retro_cmd == "on"
    if a.job:
        b = _require_board(p, a.job)
        if retro.is_retro(b.goal_id):
            _die(f"{b.goal_id} 是复盘 Job，不能再开复盘")
        b.retro = on
        b.save(p)
        what = f"Job {b.goal_id}" + ("（已收敛：下一次 tick/monitor 即补建复盘）" if on and b.is_complete() else "")
    elif a.plan:
        f = _plan_file(p, a.plan)
        retro.set_plan_flag(f, on)
        what = f"流程模板 {f.stem}（之后 `plan` 载入的 Job 生效；已载入的 Job 用 --job 改）"
    else:
        retro.set_global(p, on)
        what = "全局总闸 team.yaml retro.enabled"
    print(f"retro {'on' if on else 'off'}: {what}")
    v = _retro_view(p)
    if on and not v["enabled"]:
        print("note: 全局总闸未开（`team retro on`），流程开关暂不生效", file=sys.stderr)
    if on and not v["has_retro_agent"]:
        print(f"note: 没有声明 {retro.RETRO_CAP} 能力的 agent，复盘无人可派", file=sys.stderr)
    return 0


def cmd_retro_run(a) -> int:
    """手动为一个已收敛 Job 建复盘 Job（不受两级开关约束，但仍要有 flow-retro agent）。"""
    p = _paths()
    b = _require_board(p, a.goal_id)
    if retro.is_retro(b.goal_id):
        _die(f"{b.goal_id} 本身是复盘 Job，不再复盘")
    if not b.is_complete():
        _die(f"Job {b.goal_id} 尚未收敛（{b.summary()}），收敛后再复盘")
    if not retro.has_retro_agent(p):
        _die(f"没有声明 {retro.RETRO_CAP} 能力的 agent，复盘 Job 无人可派——先建/登记 flow-retro agent")
    rgid = retro.spawn(p, b, reason="manual")
    retro.record_manual(p, b, rgid)
    print(f"created retro job {rgid}——`team tick --job {rgid}` 派出，收敛后 `team insights list` 查看建议")
    return 0


def _insight_line(it: dict) -> str:
    rec = f" ×{it['occurrences']}" if it["occurrences"] > 1 else ""
    return (f"[{it['id']}] {it['score']:>4}  {it['scope']}/{it['category']}  "
            f"{it['impact']}影响/{it['effort']}改动{rec}  {it['title']}  → {it['target'] or '—'}")


def cmd_insights_list(a) -> int:
    p = _paths()
    ing = retro.ingest(p)                 # 先把已完成复盘的新建议收进池，看到的永远是最新
    items = retro.ranked(p, status=None if a.status == "all" else a.status,
                         scope=a.scope or None, top=a.top or None)
    if a.json:
        _print({"ingested": ing, "items": items})
        return 0
    if ing["sources"]:
        print(f"（新收入 {len(ing['sources'])} 份复盘：新增 {ing['new']} 条、复现 {ing['recurred']} 条、"
              f"重开 {ing['reopened']} 条）")
    for f in ing["malformed"]:
        print(f"warning: 复盘产物格式不对，已跳过：{f}", file=sys.stderr)
    print("\n".join(_insight_line(it) for it in items) or "（建议池为空）")
    return 0


def cmd_insights_show(a) -> int:
    p = _paths()
    retro.ingest(p)
    hit = [it for it in retro.ranked(p, status=None) if it["id"] in a.ids]
    if not hit:
        _die(f"未找到建议 {a.ids}")
    _print(hit)
    return 0


def cmd_insights_mark(a) -> int:
    missing = retro.mark(_paths(), a.ids, a.status, note=a.note or "")
    if missing:
        print(f"未找到建议 {missing}", file=sys.stderr)
    done = [i for i in a.ids if i not in missing]
    if done:
        print(f"marked {a.status}: {', '.join(done)}")
    return 0 if done else 1


@_locks_job("goal_id")
def cmd_skip(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    res = coordinator.bypass_tasks(p, b, a.task_id, reason=a.reason or "")
    _print(res)
    return 0 if res.get("bypassed") else 1   # 一个都没跳成(全 not_found/终态)时非零退出，与 retask/reject 一致


@_locks_job("goal_id")
def cmd_retask(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    try:
        res = coordinator.retask_task(p, b, a.task_id, new_goal=a.goal, append=a.append,
                                      expect=a.expect, force=a.force, reason=a.reason or "")
    except ValueError as e:              # 非法输入(缺 --goal/--append、空 goal、残留槽)：一行 stderr，勿吐 traceback
        _die(f"retask 非法：{e}")
    _print(res)
    return 0 if res.get("retasked") else 1


@_locks_job("goal_id")
def cmd_answer(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    res = coordinator.answer_task(p, b, a.task_id, a.text)
    _print(res)
    return 0 if res.get("ok") else 1


@_locks_job("goal_id")
def cmd_reject(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    res = coordinator.apply_reject(p, b, a.target, from_task=a.from_task or "",
                                   reason=a.reason or "", evidence=a.evidence or [],
                                   force=a.force)
    _print(res)
    if res.get("invalid_from"):
        _die(f"reject 非法：--from {a.from_task!r} 不是本 Job 的任务，拒绝把脏来源写进永久凭证")
    if res.get("rejected_terminal"):     # 打回 BYPASSED/SKIPPED 终态：无「已产出可重做」成果，明确挡回
        _die(f"reject 非法：{a.target} 是 bypassed/skipped 终态（有意跳过 / 已展开的 fan-out 容器 / 不可达），"
             f"没有可重做的产出，不能打回。要重跑请对真正跑过的 done/failed 上游打回，或重新 plan。")
    if res.get("escalate_no_owner"):     # 触顶但无可承接裁决者：不静默冻结，非零退出并给出可操作指引
        print(f"reject 已达上限：{a.target} 被打回 {res.get('reject_count')} 次仍未通过，且未指定可承接裁决的 "
              f"--from。请用 `--force` 越限再给一次机会（可先 retask {a.target} 加强指导），或按失败收尾。",
              file=sys.stderr)
        return 1
    return 0 if (res.get("applied") or res.get("escalated")) else 1


@_locks_job("goal_id")
def cmd_expand(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    t = b.tasks.get(a.template)
    if t is None:
        _die(f"expand 失败：节点 {a.template!r} 不存在（用 `status {a.goal_id}` 查看）")
    if not t.fanout:
        _die(f"expand 失败：节点 {a.template!r} 不是 fan-out 模板（plan 里未声明 fanout）")
    shard_var = t.fanout.get("shard_var", "shard")
    total_var = t.fanout.get("total_var", "total")
    extra = _parse_vars(a)                       # --var/--vars-file 里的额外槽（可选）
    # 每片 goal 的来源：--goals-file 给的显式列表（每片不同指令），否则 clone 模板 goal 逐片替换分片号
    children_goals: list[str]
    children_expects: list[str] | None = None
    if a.goals_file:
        loaded = _load_json(a.goals_file, "--goals-file")
        if not isinstance(loaded, list) or not loaded:
            _die("expand 失败：--goals-file 必须是非空 JSON 数组（每片一个 goal 字符串或对象）")
        children_goals, children_expects = [], []
        for it in loaded:
            if isinstance(it, str):
                g, e = it, t.expect
            elif isinstance(it, dict) and it.get("goal"):
                g, e = str(it["goal"]), str(it.get("expect", t.expect))
            else:
                _die(f"expand 失败：--goals-file 每项须为 goal 字符串或含 goal 的对象，得到 {it!r}")
            if not g.strip():                 # 空 / 纯空白 goal 与「绝不把半成品发给 worker」原则冲突，一律拒
                _die(f"expand 失败：--goals-file 里存在空/纯空白的 goal（第 {len(children_goals)+1} 项）")
            children_goals.append(g)
            children_expects.append(e)
        k = len(children_goals)
    else:
        if not a.count or a.count < 1:
            _die("expand 失败：需给 --count K（K>=1），或用 --goals-file 提供每片 goal")
        k = a.count
        children_goals, children_expects = [], []
        for i in range(1, k + 1):
            vals = {**extra, shard_var: str(i), total_var: str(k)}
            children_goals.append(_substitute(t.goal, vals))
            children_expects.append(_substitute(t.expect, vals))
    # 半成品 goal 绝不发给 worker：替换后仍残留 {{}} 直接拒（与 plan/retask 守卫共用兜底正则）
    leftover = sorted({m for g in children_goals for m in UNFILLED_SLOT_RE.findall(g)})
    if leftover:
        _die(f"expand 失败：子任务 goal 仍有未填占位槽 {leftover}——"
             f"模板 goal 里除 {{{{{shard_var}}}}}/{{{{{total_var}}}}} 外的槽需用 --var/--vars-file 补齐")
    res = coordinator.expand_fanout(p, b, a.template, children_goals, children_expects,
                                    reason=a.reason or "")
    _print(res)
    if not res.get("ok"):
        print(f"expand 失败：{res.get('error')}", file=sys.stderr)
        return 1
    return 0


def cmd_dashboard(a) -> int:
    from . import dashboard          # 延迟导入：只有真开面板才拉起 http.server
    dashboard.serve(_paths(), port=a.port, host=a.host)
    return 0


@_locks_job("goal_id")
def cmd_job_remove(a) -> int:
    p = _paths()
    b = _require_board(p, a.goal_id)
    closed = coordinator.close_goal_panes(p, a.goal_id)   # 先回收在途 pane + 剔账本，防孤儿
    b.delete(p)
    # 顺带清掉 file_lock 遗留的 <gid>.json.lock 侧文件——否则 jobs/ 目录长期堆积僵尸锁文件。
    with contextlib.suppress(OSError):
        p.job_file(a.goal_id).with_suffix(".json.lock").unlink(missing_ok=True)
    notes = []
    if a.purge:
        _clear_goal_signal_files(p, a.goal_id)
        # 无剩余活跃 Job 时，回收框架自建的空闲 team workspace
        if not [x for x in Board.load_all(p) if not x.is_complete()]:
            coordinator.reap_team_workspace(p)
        notes.append("含信号")
    # 产物是交付物，只有显式 --delete-artifacts 才删；--purge 不再连带删产物
    if a.delete_artifacts:
        jart = p.job_artifacts_dir(a.goal_id)   # Job 产物目录
        if jart.exists():
            shutil.rmtree(jart)
        notes.append("含产物")
    extra = f"，回收 {closed} 个在途 pane" if closed else ""
    tag = f"（{'、'.join(notes)}）" if notes else ""
    print(f"removed job {a.goal_id}" + tag + extra)
    return 0


def _add_job_flags(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """tick/monitor/run 共用的 --job/--dry-run 声明，避免三处重复。"""
    parser.add_argument("--job", default="",
                        help="限定单个 Job(goal_id)；省略则跨所有活跃 Job")
    parser.add_argument("--dry-run", action="store_true",
                        help="只写信号、不拉真实 pane（本地验证）")
    return parser


def _add_var_flags(parser: argparse.ArgumentParser, var_help: str, file_help: str) -> None:
    """plan/expand 共用的 --var/--vars-file 声明（由 _parse_vars 合并）。"""
    parser.add_argument("--var", action="append", metavar="KEY=VALUE", help=var_help)
    parser.add_argument("--vars-file", dest="vars_file", metavar="PATH", help=file_help)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="team", description="agent-team 编排 CLI")
    ap.add_argument("--log-level", dest="log_level", default=None,
                    metavar="LEVEL",
                    help="库层日志级别(DEBUG/INFO/WARNING/ERROR)，默认取环境变量 TEAMS_LOG 或 WARNING；日志走 stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ag = sub.add_parser("agent", help="agent 维护").add_subparsers(
        dest="sub", required=True)
    c = ag.add_parser("create")
    c.add_argument("name")
    c.add_argument("--role", default="")
    c.add_argument("--capabilities", default="")
    c.add_argument("--max-concurrent", type=int, default=1, dest="max_concurrent")
    c.set_defaults(func=cmd_agent_create)
    ag.add_parser("list").set_defaults(func=cmd_agent_list)
    s = ag.add_parser("show")
    s.add_argument("name")
    s.set_defaults(func=cmd_agent_show)
    r = ag.add_parser("remove")
    r.add_argument("name")
    r.add_argument("--purge", action="store_true")
    r.set_defaults(func=cmd_agent_remove)

    pl = sub.add_parser("plan", help="载入一个任务 DAG 为 Job(按 goal_id)；支持 {{槽}} 模板")
    pl.add_argument("file")
    pl.add_argument("--as", "--goal-id", dest="as_goal", metavar="GOAL_ID",
                    help="覆写 goal_id，把可复用 plan 模板实例化成独立 Job（可并发起多个实例）")
    _add_var_flags(pl, "填充模板占位槽 {{KEY}}；可重复。适合短值",
                   "从 JSON 对象(key->value)批量填充占位槽；适合协调者写好的多行阶段目标")
    pl.add_argument("--show-vars", dest="show_vars", action="store_true",
                    help="只列出该模板需要填充的占位槽，不载入 Job")
    pl.set_defaults(func=cmd_plan)
    sub.add_parser("jobs", help="列出所有 Job").set_defaults(func=cmd_jobs)
    st = sub.add_parser("status", help="查看 Job 状态(省略 goal 则列全部)")
    st.add_argument("goal_id", nargs="?")
    st.set_defaults(func=cmd_status)
    tk = sub.add_parser("tick", help="调度一轮(默认跨所有活跃 Job)")
    tk.add_argument("--no-hold", action="store_true", dest="no_hold",
                    help="本轮收割才解锁的下游也当场派出（默认留进 held，等协调者读完上游摘要再 tick）")
    _add_job_flags(tk).set_defaults(func=cmd_tick)
    mo = sub.add_parser("monitor", help="监控 outbox 一次(默认所有活跃 Job)")
    _add_job_flags(mo).set_defaults(func=cmd_monitor)
    wt = sub.add_parser("wait", help="阻塞到在途任务有状态变化或超时(只收割不分派)，逐轮驱动用它替代 sleep+monitor")
    wt.add_argument("--job", default="", help="限定单个 Job(goal_id)；省略则跨所有活跃 Job")
    wt.add_argument("--timeout", type=float, default=coordinator.WAIT_TIMEOUT,
                    help="最长阻塞秒数(默认 %(default)s，留在 shell 120s 超时内)")
    wt.add_argument("--poll", type=float, default=coordinator.WAIT_POLL, help="收割间隔秒数")
    wt.add_argument("--since", type=int, default=None,
                    help="事件游标(上次 wait 返回的 cursor)：返回此后的事件")
    wt.set_defaults(func=cmd_wait)
    wa = sub.add_parser("watch", help="前台跑后台收割守护（tick 有真实分派时会自动脱离会话拉起；"
                                      "只收割/判超时/回收、从不分派；无在途任务一段时间后自行退出）")
    wa.add_argument("--poll", type=float, default=5.0, help="收割间隔秒数")
    wa.add_argument("--idle-exit", type=float, default=300.0, dest="idle_exit",
                    help="连续多少秒无在途任务即退出")
    wa.set_defaults(func=cmd_watch)
    rn = sub.add_parser("run", help="自动循环调度活跃 Job 直到完成/卡死(默认全部，--job 限定单个)")
    rn.add_argument("--interval", type=float, default=3.0)
    rn.add_argument("--max-ticks", type=int, default=200, dest="max_ticks")
    _add_job_flags(rn).set_defaults(func=cmd_run)
    rp = sub.add_parser("report", help="汇总结果(省略 goal 则聚合全部)")
    rp.add_argument("goal_id", nargs="?")
    rp.add_argument("--timing", action="store_true",
                    help="改为打印计时文本表：每步排队/拉起/净执行/等人 + 按 agent 聚合耗时")
    rp.set_defaults(func=cmd_report)
    sk = sub.add_parser("skip",
                        help="主动跳过(绕过)某步骤：判定无需执行，标记 bypassed；"
                             "下游把它视同已满足、照常执行(区别于上游失败的级联 skipped)")
    sk.add_argument("goal_id")
    sk.add_argument("task_id", nargs="+", help="要跳过的 task_id(可多个)")
    sk.add_argument("--reason", default="",
                    help="跳过理由：写入该任务 summary，供 report/看板复盘")
    sk.set_defaults(func=cmd_skip)
    rt = sub.add_parser("retask",
                        help="推进中改写某步的 goal：读上游摘要后为「未派发的下游」现改指令，"
                             "或加 --force 回收正在跑的那步重派(goal 分派即冻结，故在跑需 force)")
    rt.add_argument("goal_id")
    rt.add_argument("task_id")
    rt.add_argument("--goal", default=None, help="全量替换该步 goal")
    rt.add_argument("--append", default=None, help="在原 goal 末尾追加一段(与 --goal 二选一)")
    rt.add_argument("--expect", default=None, help="同时更新期望产物 expect(可选)")
    rt.add_argument("--force", action="store_true",
                    help="对已 dispatched(在跑)的任务：先回收 pane/信号/账本，改完 goal 重置为待派、下轮重派")
    rt.add_argument("--reason", default="", help="改写理由：记入事件日志，供复盘")
    rt.set_defaults(func=cmd_retask)
    an = sub.add_parser("answer",
                        help="回答某个 awaiting_input 任务：把答复注入 worker pane，让它继续跑")
    an.add_argument("goal_id")
    an.add_argument("task_id")
    an.add_argument("text", help="给 worker 的答复文本")
    an.set_defaults(func=cmd_answer)
    rj = sub.add_parser("reject",
                        help="手动打回某步：把目标节点连同其全部后代重置重跑，并注入打回意见+证据为凭证；"
                             "评审/校验类 worker 也可自行在 outbox 写 status=reject 触发(monitor 自动执行)")
    rj.add_argument("goal_id")
    rj.add_argument("target", help="被打回的目标 task_id（从它起重跑）")
    rj.add_argument("--from", dest="from_task", default="",
                    help="打回来源 task_id（记入凭证；worker 自动打回时即评审节点）")
    rj.add_argument("--reason", default="", help="打回意见：作为凭证注入目标，令其针对性修复、勿重复犯错")
    rj.add_argument("--evidence", action="append", default=[],
                    help="证据文件绝对路径（可重复），随凭证注入目标供其核对")
    rj.add_argument("--force", action="store_true",
                    help="越过打回次数上限强制再打回一次；并可救活已升级(escalated)parked 的打回者")
    rj.set_defaults(func=cmd_reject)
    ex = sub.add_parser("expand",
                        help="动态展开一个 fan-out 模板节点为 K 个并行子任务（协调者读上游产出后调）："
                             "生成 <node>.1..<node>.K、重接下游依赖、模板置 bypassed 容器；"
                             "子任务同组共享 max_parallel 并发闸（宽度不限、只限同时并发）")
    ex.add_argument("goal_id")
    ex.add_argument("template", help="要展开的 fan-out 模板 task_id")
    ex.add_argument("--count", type=int, default=0,
                    help="展开成几个子任务 K（K>=1）；与 --goals-file 二选一")
    ex.add_argument("--goals-file", dest="goals_file", metavar="PATH",
                    help="JSON 数组：每片一个 goal 字符串或 {goal,expect} 对象（每片指令不同时用）")
    _add_var_flags(ex, "填充子任务 goal 里除分片号外的额外占位槽；可重复",
                   "从 JSON 对象批量填充额外占位槽")
    ex.add_argument("--reason", default="", help="展开理由：记入事件日志，供复盘")
    ex.set_defaults(func=cmd_expand)
    db = sub.add_parser("dashboard", help="启动实时看板(深蓝工作台；仅本机、无鉴权)")
    db.add_argument("--port", type=int, default=8787)
    db.add_argument("--host", default="127.0.0.1",
                    help="默认 127.0.0.1；无鉴权面板，勿绑 0.0.0.0 暴露到网络")
    db.set_defaults(func=cmd_dashboard)
    rv = sub.add_parser("retro", help="流程复盘开关（全局总闸 + 流程开关，都开才自动复盘）与手动复盘"
                        ).add_subparsers(dest="retro_cmd", required=True)
    rv.add_parser("status", help="看全局总闸、各模板与各 Job 的复盘开关").set_defaults(func=cmd_retro_status)
    for name, verb in (("on", "打开"), ("off", "关闭")):
        rt_ = rv.add_parser(name, help=f"{verb}开关：不带参数=全局总闸；带 plan=该流程模板；--job=已载入的 Job")
        rt_.add_argument("plan", nargs="?", default="", help="plan 文件路径或 coordinator/plans/ 下的模板名")
        rt_.add_argument("--job", default="", help="改已载入 Job 的开关（含历史 Job）")
        rt_.set_defaults(func=cmd_retro_toggle)
    rr = rv.add_parser("run", help="手动为一个已收敛 Job 建复盘 Job（不受开关约束）")
    rr.add_argument("goal_id")
    rr.set_defaults(func=cmd_retro_run)
    ins = sub.add_parser("insights", help="复盘建议待办池：查看/标记（team-evolve skill 据此挑建议优化项目）"
                         ).add_subparsers(dest="isub", required=True)
    il = ins.add_parser("list", help="按得分降序列出建议（先收入已完成复盘的新建议）")
    il.add_argument("--status", default="open", choices=[*retro.STATUSES, "all"])
    il.add_argument("--scope", default="", choices=["", "flow", "project"])
    il.add_argument("--top", type=int, default=0, help="只列前 N 条；0=全部")
    il.add_argument("--json", action="store_true", help="输出完整 JSON（含 problem/proposal/evidence）")
    il.set_defaults(func=cmd_insights_list)
    ish = ins.add_parser("show", help="看建议详情")
    ish.add_argument("ids", nargs="+")
    ish.set_defaults(func=cmd_insights_show)
    im = ins.add_parser("mark", help="标记建议状态：applied=已落地 / dismissed=不采纳 / open=重开")
    im.add_argument("status", choices=retro.STATUSES)
    im.add_argument("ids", nargs="+")
    im.add_argument("--note", default="", help="备注：落地了什么 / 为何不采纳")
    im.set_defaults(func=cmd_insights_mark)
    jr = sub.add_parser("job", help="Job 管理").add_subparsers(dest="jsub", required=True)
    jrm = jr.add_parser("remove")
    jrm.add_argument("goal_id")
    jrm.add_argument("--purge", action="store_true",
                     help="连带清理该 Job 的陈旧信号并回收空闲 team workspace（不删产物）")
    jrm.add_argument("--delete-artifacts", action="store_true", dest="delete_artifacts",
                     help="额外删除该 Job 的产物目录（交付物，慎用；默认保留）")
    jrm.set_defaults(func=cmd_job_remove)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    _setup_logging(getattr(a, "log_level", None))
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
