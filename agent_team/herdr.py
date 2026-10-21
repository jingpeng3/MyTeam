"""herdr 集成层：管 pane 的生命周期（拉起 / 探活 / 回收）。

遵循 herdr skill 的约定（`herdr --skill`）：
- 从文档化字段解析 ID（workspace.workspace_id / root_pane.pane_id / pane.pane_id），
  不靠预测或脆弱的深度搜索。
- `agent start` 需要一个已在交互 shell 提示符的既有 pane，且用唯一、合法的 agent 名。
- 后台工作一律 `--no-focus`，只回收自己创建的 pane。

本框架有意偏离 skill「默认在当前 tab 同级 split」的默认：team 的 worker 统一落在
team.yaml 指定的 workspace 下、cwd=各自 agent 工作空间，这是用户选定的团队拓扑。
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import subprocess
import time
from typing import Any

log = logging.getLogger(__name__)


def current_pane_id() -> str:
    """本协调者会话所在的 pane（herdr 在 pane 环境里注入）。绝不能被回收。"""
    return os.environ.get("HERDR_PANE_ID", "")


def current_workspace_id() -> str:
    """本协调者会话所在的 workspace。团队 workspace 若解析到它，绝不能被 close。"""
    return os.environ.get("HERDR_WORKSPACE_ID", "")


# herdr 支持多 session（default/其它）；team 的 worker 全部跑在协调者所在的那个 session 里。
# `herdr` CLI 默认连 default session，只有 `--session <name>`（或 HERDR_SESSION 环境变量）才切到
# 别的 session。协调者进程继承了 HERDR_SESSION，故裸调也命中；但 dashboard 是**独立进程**，常被
# 从没有该环境变量的终端拉起 → 它的 herdr 子进程落到 default session，看不到 team 的 worker，
# 对每个 worker 都报 agent_not_found。为此：_run 统一按「显式入参 > 本进程 HERDR_SESSION」解析出
# session 并注入 `--session`，让 session 定位不再看脸吃环境；dashboard 再把每个 worker 分派时
# 记录的 session 显式传进来（见 read_agent / coordinator._commit_dispatch）。
_DEFAULT_SESSION = os.environ.get("HERDR_SESSION", "").strip()


def current_session() -> str:
    """本进程启动时继承的 herdr session 名（空=未设置，即 default session）。
    分派 worker 时记进 runtime 账本，供跨进程的 dashboard 回放定位同一 session。"""
    return _DEFAULT_SESSION


# herdr agent_status 的语义分组：存活推进中 / turn 已结束可接收输入 / 会话已死
ALIVE_STATES = frozenset({"working", "busy", "running", "active", "thinking"})
READY_STATES = frozenset({"idle", "done"})
DEAD_STATES = frozenset({"error", "crashed", "gone", "dead"})


class HerdrError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class HerdrRetryable(HerdrError):
    """可重试的瞬时错误（如 agent_pane_busy：新建 pane 的 shell 还没到提示符）。"""


# herdr 返回的、属于「等一下再来就好」的瞬时错误码
RETRYABLE_CODES = {"agent_pane_busy", "pane_not_ready", "agent_not_ready"}


def _raise_if_error(prefix: str, data: Any, unknown: str = "unknown") -> None:
    """data 若是 herdr 错误体 `{"error": {code, message}}`，按错误码抛 HerdrRetryable/HerdrError。"""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = err.get("code", "")
        msg = err.get("message", "") or code or unknown
        exc = HerdrRetryable if code in RETRYABLE_CODES else HerdrError
        raise exc(f"{prefix} [{code}]: {msg}", code=code)


def _raise_from_payload(prefix: str, text: str) -> None:
    """若 text 是 herdr 错误 JSON，按错误码抛 HerdrRetryable/HerdrError。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise HerdrError(f"{prefix}: {text}") from None
    _raise_if_error(prefix, data)
    raise HerdrError(f"{prefix}: {text}")


def _run(args: list[str], timeout: int = 60, session: str | None = None) -> dict[str, Any]:
    prefix = f"herdr {' '.join(args[:2])} failed"
    # session 定位：显式入参优先（dashboard 从账本回放时传），否则用本进程继承的 HERDR_SESSION。
    # 解析出非空 session 时注入全局 `--session`，不再依赖子进程是否恰好继承了对的环境变量。
    sess = (session or "").strip() or _DEFAULT_SESSION
    cmd = ["herdr", "--session", sess, *args] if sess else ["herdr", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        # herdr 未安装/不在 PATH：给出可操作的报错，而非让原始 FileNotFoundError 裸穿
        raise HerdrError(
            "找不到 `herdr` 可执行文件：请确认 herdr 已安装并在 PATH 中",
            code="herdr_not_found",
        ) from None
    except subprocess.TimeoutExpired:
        # 命令挂死超时属瞬时故障，标 retryable 让上层的退避/重试逻辑接管
        raise HerdrRetryable(f"{prefix}: 命令超时（>{timeout}s）", code="herdr_timeout") from None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        # 错误体常打在 stderr（如 agent start 的 agent_not_ready）；解析出错误码分类
        _raise_from_payload(prefix, proc.stderr.strip())
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        if proc.returncode != 0:
            _raise_from_payload(prefix, proc.stderr.strip() or out)
        return {"_raw": out, "_stderr": proc.stderr.strip()}
    # herdr 也可能把错误体打在 stdout：统一识别，避免下游拿到误导性的空 pane_id
    _raise_if_error(prefix, data, unknown="unknown herdr error")
    return data.get("result", data)


def sanitize_name(raw: str) -> str:
    """转成合法 herdr agent 名：[a-z][a-z0-9_-]{0,31}。

    超长时**不能从尾部直接截断**——调用方把唯一性后缀（如 :<task_id>:a<attempt>）放在尾部，
    直接 `[:32]` 会把它切掉，导致同一 agent 承接的多个并行任务名坍缩成同名 → agent_name_taken。
    这里改为「保留头部可读前缀 + 附加全名 hash」，让唯一性必然存活。
    """
    s = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-")
    if not s or not s[0].isalpha():
        s = "a" + s
    if len(s) <= 32:
        return s
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]  # 全名 hash：随 task_id/attempt 变化
    head = s[:25].rstrip("-")
    return f"{head}-{h}"[:32]


def _pane_id_from(obj: Any) -> str | None:
    """按文档化字段取 pane_id：root_pane / pane / 顶层 pane_id；末位再深搜兜底。"""
    if isinstance(obj, dict):
        for key in ("root_pane", "pane"):
            node = obj.get(key)
            if isinstance(node, dict) and isinstance(node.get("pane_id"), str):
                return node["pane_id"]
            if isinstance(node, str):
                return node
        if isinstance(obj.get("pane_id"), str):
            return obj["pane_id"]
    return _deep_find(obj, "pane_id")


def _deep_find(obj: Any, key: str) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str):
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, key)
            if r:
                return r
    return None


def find_workspace(label: str) -> str | None:
    res = _run(["workspace", "list"])
    for ws in res.get("workspaces", []):
        if ws.get("label") == label:
            return ws.get("workspace_id")
    return None


def ensure_workspace(label: str, cwd: str) -> str:
    wid = find_workspace(label)
    if wid:
        return wid
    res = _run(["workspace", "create", "--label", label, "--cwd", cwd, "--no-focus"])
    ws = res.get("workspace")
    wid = ws.get("workspace_id") if isinstance(ws, dict) else _deep_find(res, "workspace_id")
    if not wid:
        wid = find_workspace(label)
    if not wid:
        raise HerdrError(f"无法创建/定位 workspace {label}")
    return wid


def create_pane(workspace_id: str, cwd: str, label: str,
                env: dict[str, str] | None = None) -> str:
    """在指定 workspace 新建 tab（含一个 root pane），返回其 pane_id。"""
    args = ["tab", "create", "--workspace", workspace_id,
            "--cwd", cwd, "--label", label, "--no-focus"]
    for k, v in (env or {}).items():
        args += ["--env", f"{k}={v}"]
    res = _run(args)
    pane_id = _pane_id_from(res)
    if not pane_id:
        raise HerdrError(f"tab create 未返回 pane_id: {res}")
    return pane_id


def _wait_agent_ready(name: str, timeout_s: float = 45.0) -> bool:
    """轮询 agent 状态直到可接受输入（idle/done）。用于 agent_not_ready 后等待启动完成。"""
    deadline = time.time() + timeout_s
    delay = 0.5
    while time.time() < deadline:
        try:
            st = agent_status(name)
        except HerdrError:
            st = "unknown"
        if st in READY_STATES:
            return True
        if st in DEAD_STATES:
            return False
        time.sleep(delay)
        delay = min(delay * 1.5, 3.0)
    return False


def start_agent(pane_id: str, name: str, kind: str,
                extra_args: list[str] | None = None,
                timeout_ms: int = 60000, retries: int = 4,
                model: str = "") -> str:
    """在既有 shell pane 上启动 agent；成功返回后 agent 已就绪。返回 agent 名。

    `model` 非空时作为 CLI 参数注入到底层 agent（`herdr agent start` 本身无 --model，
    只把 `--` 之后的参数透传给底层 CLI；claude 认 `--model <model>`）；置于 flags 前，
    manifest 里显式写的 flags 可覆盖同名项。

    真实执行的两类头号瞬时错误（压测实测）都在这里消化，不再把偶发抖动放大成整轮崩溃：
    - `agent_pane_busy`：新建 tab 的 shell 还没到提示符 → 指数退避重试 `agent start`。
    - `agent_not_ready`：agent 已在但启动中（herdr 判为 blocked）→ 名字已注册，
      轮询其状态直到 idle/done 即可，无需重启。
    """
    name = sanitize_name(name)
    args = ["agent", "start", name, "--kind", kind, "--pane", pane_id,
            "--timeout", str(timeout_ms)]
    agent_args: list[str] = []
    if model:
        agent_args += ["--model", model]
    if extra_args:
        agent_args += list(extra_args)
    if agent_args:
        args += ["--", *agent_args]
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            _run(args, timeout=timeout_ms // 1000 + 15)
            return name
        except HerdrRetryable as e:
            if e.code == "agent_not_ready":
                # agent 进程已起、只是启动 UI 未就绪；等它变 idle 而不是重启
                if _wait_agent_ready(name):
                    return name
                if attempt >= retries:
                    raise
            elif attempt >= retries:
                raise
            log.debug("agent start %s 瞬时失败(%s)，退避 %.1fs 后第 %d/%d 次重试",
                      name, e.code or "?", delay, attempt + 1, retries)
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
    return name


def prompt_agent(target: str, text: str, wait: bool = False,
                 until: list[str] | None = None, timeout_ms: int = 0) -> dict:
    """向 agent 提交 prompt。分派场景用 fire-and-forget（wait=False），
    完成靠 outbox 信号判定，不阻塞在这里等 turn 结束。"""
    args = ["agent", "prompt", target, text]
    if wait:
        args.append("--wait")
    for u in (until or []):        # 仅特定状态工作流才用 --until
        args += ["--until", u]
    if timeout_ms:
        args += ["--timeout", str(timeout_ms)]
    return _run(args, timeout=(timeout_ms // 1000 + 15) if timeout_ms else 120)


def agent_status(target: str) -> str:
    """返回 herdr 上报的 agent_status；agent 已不存在(agent_not_found)时归一化为 "gone"，
    让上层按「已死」**立即**回收/重试，而不是把「被删/被杀」和「查询抖动」一样当 unknown、
    苦等满 timeout 才判死。其它 HerdrError（真抖动）仍照常抛出，由调用方按 unknown 保守处理。"""
    try:
        return _deep_find(_run(["agent", "get", target]), "agent_status") or "unknown"
    except HerdrError as e:
        if getattr(e, "code", "") == "agent_not_found":
            return "gone"
        raise


def read_agent(target: str, lines: int = 120, session: str | None = None) -> str:
    """读某 agent pane 的近段输出。
    优先 recent-unwrapped（idle 时能取到完整不换行的 scrollback 历史）；worker 正在干活时
    alt-screen 历史读不了(agent_not_idle)，退回 --source visible 取当前可见屏——正是 live 视图要的。

    `session`：worker 所在的 herdr session（dashboard 从 runtime 账本取出显式传入），缺省时
    由 _run 回退到本进程的 HERDR_SESSION。跨进程读取务必带上，否则会落到 default session 报
    agent_not_found。"""
    try:
        res = _run(["agent", "read", target, "--source", "recent-unwrapped",
                    "--lines", str(lines)], session=session)
    except HerdrError as e:
        if getattr(e, "code", "") != "agent_not_idle":
            raise
        res = _run(["agent", "read", target, "--source", "visible",
                    "--lines", str(lines)], session=session)
    return res.get("_raw") or json.dumps(res, ensure_ascii=False)


def close_pane(pane_id: str) -> None:
    _run(["pane", "close", pane_id])


def workspace_pane_count(workspace_id: str) -> int:
    """该 workspace 当前的 pane 数（用于判断是否只剩空闲 root pane 可回收）。"""
    res = _run(["pane", "list", "--workspace", workspace_id])
    panes = res.get("panes") if isinstance(res, dict) else None
    return len(panes) if isinstance(panes, list) else 0


def close_workspace(workspace_id: str) -> None:
    """关闭整个 workspace（含其 root pane）。仅用于回收框架自建的 team workspace。"""
    _run(["workspace", "close", workspace_id])
