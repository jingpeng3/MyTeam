# Agent Team (TEAMS)

基于 **herdr** 的多 agent 团队编排框架。把一句话目标拆成任务 DAG，按需在 herdr pane 里拉起 worker 并行执行，用文件信号收敛结果——**当前交互会话就是协调者**。

> 💬 **全程可对话，不用敲命令**（`.claude/skills/` 下三个 skill）：
> - 搭团队 / 编排 → 「建个团队 / 帮我编排 XX」→ `team-build`（澄清目标 → 建 agent → 写 plan → dry-run → 真跑）。
> - 只加一个 agent → 「建个 / 加个 agent」→ `agent-create`。
> - 执行流程 → 「跑起来 / 继续跑 / 让团队干活」→ `team-run`（协调者逐轮 tick+monitor 驱动到完成并汇报）。

## 核心特性

**声明式 agent**
每个 agent 就是一个目录，通过一份 manifest 声明它的身份、运行时、擅长的能力和并发上限。想调整某个 agent 的行为，改它的声明即可，团队注册表统一管理所有成员和默认运行时。

**协调者常驻、worker 按需拉起**
当前交互会话就是协调者，长期在线；worker 只在有任务时才被拉起，干完立即回收。协调者靠结构化的任务/结果信号判断进度，不去猜 worker 终端里输出了什么；所有状态都落盘，随时可恢复，不依赖记忆。

**多目标并行、彼此隔离**
每个目标是一个独立的 Job，各自维护状态、结果和产物，互不覆盖。一轮调度会跨所有活跃 Job 同时推进；单个 agent 的并发上限在每个 Job 内独立结算，两个 Job 都能各自跑满、互不挤占。

**任务编排与能力路由**
用一张任务 DAG 描述目标：每个节点声明依赖和所需能力。依赖满足的节点会按能力精确派给擅长的 agent 并行执行，找不到合适的 agent 就排队等待，绝不误派。

**可复用的流程模板**
流程可以写成模板——固定编排骨架，把目标、阶段描述等留成待填的槽位。同一个模板能实例化成多个不同目标的任务并同时运行。缺哪个槽没填会直接拦下报错，不会把半成品目标发给 worker。

**产物共享与按需继承**
跨任务的产物集中存放并按目标隔离，写产物和读产物都会自动定位到当前 Job 的目录。下游节点可以显式声明要读上游哪几个节点的产物，框架自动把对应文件递给它，不需要的默认不继承。

**失败重试、跳过与容错**
worker 报告失败会在重试上限内自动重排，超限才判定失败；整个流程卡住无法推进时会自动停下等待处理。协调者也能主动跳过某个已无需执行的步骤，下游照常进行。worker 回传的结果即使被额外文字包裹也能被稳健解析，真正无法解析的才会保留下来以便排查。

**worker 交互与实时看板**
worker 遇到需要澄清的地方可以回问，协调者把答复回注后它继续执行。还内置一个本机实时看板，可视化各目标的任务 DAG、进度状态与流程模板。

## 目录结构

```
team.yaml              团队注册表 + 全局默认运行时
coordinator/
  prompt.md            协调者职责说明
  plans/               plan 模板 / 每个目标的 DAG
    vars/              --vars-file 填槽值（git 忽略）
  state/               jobs/<goal_id>.json、task-board、runtime pane 映射
agents/<name>/
  agent.yaml           manifest（身份/运行时/capabilities/并发）
  prompt.md            该 agent 的 persona
  workspace/           agent 工作目录（基座只读参考；worker cwd 为其下
                       <goal_id>__<task_id>/ 本任务独占临时空间，任务结束即清）
  inbox/  outbox/      任务信号 / 结果信号
shared/artifacts/      跨 agent 共享产物（按 goal_id 分子目录）
agent_team/            核心 Python 包
tests/                 pytest 用例
pyproject.toml         依赖声明 + ruff/pytest 配置
```

## 依赖与安装

**1. Python 环境**

- Python 3.11+
- PyYAML —— 唯一第三方运行时依赖（其余全用标准库），版本钉在 `pyproject.toml`。

安装 `team` CLI（skill 与协调者都只用它）：

```bash
scripts/install.sh           # 安装/重装 team CLI（可重复执行）
scripts/install.sh --dev     # 另建开发环境 .venv（pytest/ruff）
scripts/install.sh --uninstall
```

脚本用 `uv tool install --editable .`（没有 uv 时退回 pipx）为 CLI 建独立环境、把 `team` 装到 `~/.local/bin`，
装完自检 `team` 是否在 PATH 上、能否跑通，并提示是否缺 herdr。不用 `pip install`：Homebrew 等系统 Python 是
externally-managed（PEP 668），直接装会被拒。editable 安装下改代码即时生效；pyproject 改了依赖/入口后重跑一次脚本。

开发自检：`uv run --extra dev pytest` 跑用例，`uv run --extra dev ruff check` 做静态检查。

**2. herdr CLI（必装）**

worker 靠 herdr 拉起，运行本框架必须先装好 herdr 并保证在 `PATH` 中。

```bash
# macOS (Homebrew)
brew install herdr

# 之后升级
herdr update
```

其他平台的安装方式见官网 <https://herdr.dev>。装好后 `herdr --version` 应能正常输出。

**3. 在 herdr 里、于项目当前目录启动协调者**

在项目根目录（本仓库）下启动 herdr，拉起交互 agent（Ducc / Claude Code）——它就是协调者，无需设置任何环境变量。之后整个流程由 skill 驱动、全程对话完成：

- **建流程** → 说「建个团队 / 帮我编排 XX」（`team-build`），或「加个 agent」（`agent-create`），对话式澄清目标、创建 agent、写出任务流程。
- **看流程** → 启动 dashboard 实时查看各目标的任务 DAG、进度与流程模板：

  ```bash
  team dashboard                  # 默认 http://127.0.0.1:8787
  # 可选：--port 9000 换端口，--host 改监听地址
  ```

  仅本机、无鉴权，启动后浏览器打开上述地址即可。
- **跑流程** → 说「跑起来 / 继续跑 / 让团队干活」（`team-run`），协调者逐轮驱动到完成并汇报进度。

底层的 `team` CLI 是这些 skill 使用的引擎，一般不用手动敲；需要时可自行调用（`team --help` 看全部子命令，调度命令加 `--dry-run` 只写信号、不真正拉起 worker）。在仓库内任意目录都能用；仓库外调用时回退到安装它的那个仓库。

## worker 完成协议

worker 读 `inbox/<task_id>.json`，干完把结果写 `outbox/<task_id>.json`：

```json
{"task_id": "build-report", "status": "done|failed", "summary": "...", "artifacts": ["..."], "error": null}
```

协调者只认这个结构化结果，不解析 pane 自由文本。

## 已知限制

- `agent create/remove` 用 PyYAML 重写 `team.yaml`，会**丢失注释**（结构与值不变）；需保留注释可改用 ruamel.yaml。
- herdr 层的 `tab create` pane_id 提取用了深度查找兜底，返回形态请在自己环境验证。
