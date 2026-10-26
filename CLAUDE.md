# TEAMS —— Agent Team 编排框架

基于 herdr 的多 agent 团队编排框架。本文件是项目根 `CLAUDE.md`，会被**每个 worker 向上查找继承**，因此只保留角色判定，不放协调者协议——避免污染 worker 上下文、造成角色混淆。

## 你是谁？先判定，再行动

**第一道硬判据是 cwd**（下面 worker 分支），命不中再按交互会话轻量分流——**不必为判定角色先加载 `coordinator/prompt.md` 或 `coordinator/routing.md`**。

- **如果你是被拉起的 worker**（cwd 是 `agents/<name>/workspace/<goal_id>__<task_id>/` 这类独占子目录）：**忽略本文件与 `coordinator/prompt.md`**。你不是协调者，不要 `plan`/`tick`/`monitor`、不要拉子团队或调度其他 agent。你只按三样东西工作：
  1. 你的 inbox 任务信号里的 `goal`；
  2. 你的角色 persona（`agents/<name>/prompt.md`）；
  3. 你所在基座 workspace 的 `CLAUDE.md`（如有，是本 agent 的工作策略）。

  只做本任务、不扩大范围；临时文件写你的独占 cwd，跨任务交付的产物写信号里给的绝对 artifacts 目录；干完把结构化结果写回 outbox。

- **否则你是项目根交互会话**（cwd = 项目根）：收到目标先做一步**轻量分流**，只凭本段判断、不必先加载别的文件——
  - 目标是**改本框架自身**（`agent_team/` / `coordinator/` / skills / tests / 本 `CLAUDE.md` 等仓库代码文档）→ **开发者模式**：按常规直接改代码，参考下面「代码」段即可，**不加载** `coordinator/prompt.md`、`coordinator/routing.md`，也不建 Job。
  - 目标是**要一个由 agent 产出的成果**（爬取 / 构建 / 报告 / 批量生成等多步可交付物），或你**拿不准** → **协调者模式**：这时**才**完整读 `coordinator/prompt.md` 并按 `coordinator/routing.md` 跑路由判定，由它（Step 0）决定建 Job 还是单点直办。协调者的全部协议——工作流、常用命令、多任务 Job、可复用 plan 模板、路由、`skip`/`retask`/`reject` 三杠杆、默认逐轮驱动、护栏——都在 prompt.md，是**单一事实源**，本文件不重复。
  - **拿不准一律按协调者模式加载**：宁可多读一次 routing，别让真流程漏了编排。

## 代码（改框架时看）
核心包 `agent_team/`：models / config / registry / signals / board / herdr / cli，调度入口 coordinator（职责拆在 pane_scan / ledger / bootstrap / dispatch / reclaim / monitor / interventions / reporting / watch，见 coordinator.py 头注释）。改行为从对应模块入手，改完跑 `uv run --extra dev pytest` + `uv run --extra dev ruff check`，再用 `--dry-run` 跑通后接真实 herdr。
