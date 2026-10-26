---
name: team-evolve
description: 让 agent-team 项目自我增强——从流程复盘沉淀的建议待办池（coordinator/insights/backlog.json）里挑出高价值建议，核实后落地改进 plan 模板、agent prompt、路由与框架代码，并标记已采纳/驳回。当用户说"优化一下项目/团队""看看复盘建议""采纳建议""自我进化/自我增强""根据执行情况改进流程""team insights"时使用。仅在 TEAMS(agent_team)项目内适用。
---

# team-evolve —— 挑选复盘建议并落地优化

开了复盘的 Job 收敛后，框架会自动拉一个复盘 Job（`retro-<goal_id>-<时刻>`，由 `flow-retro` agent 执行），把「本流程」和「整个项目」两个层面的优化建议写进建议待办池。本 skill 负责后半段：**从池里挑出好建议、核实、落地、记账**。你是开发者角色（改框架/模板/prompt），不是协调者，不建 Job。

## 0. 前置
- 确认在 agent-team 项目根（有 `team.yaml`、`agent_team/`）。命令都在仓库根用 `uv run team X` 调用（跑本仓库 `.venv` 里的代码），不用全局安装的 `team`。
- 自动复盘有**两级开关、默认全关，都打开才复盘**：全局总闸 `team.yaml` 的 `retro.enabled`（`uv run team retro on|off`）+ 流程开关 plan 顶层 `"retro": true`（`uv run team retro on|off <模板名>`，之后载入的 Job 生效；已载入/历史 Job 用 `uv run team retro on|off --job <goal_id>`）。`uv run team retro status` 看各级开关；`uv run team retro run <goal_id>` 不看开关手动补一次。用户想让某流程以后自动复盘，就帮他开对应开关。
- 用户点名了某个 Job 但它还没复盘过：`uv run team retro run <goal_id>` 手动建复盘 Job，再按 team-run 的方式 `uv run team tick --job <复盘id>` + `uv run team wait --job <复盘id>` 驱动到收敛（真跑会拉 worker，先确认）。

## 1. 看池子
```
uv run team insights list                 # 先自动收入已完成复盘的新建议，再按得分降序列出 open 建议
uv run team insights list --scope flow    # 只看某流程/plan/agent 层面；--scope project 只看框架/通用层面
uv run team insights list --json --top 10 # 带 problem/proposal/evidence 全文
uv run team insights show <id...>
```
得分 = 影响×置信 + 复现加成（多次复盘都出现）+ 改动量加减。`×N` 表示 N 次复盘都提到同一问题。

## 2. 挑建议
按用户意图筛（用户指定了流程/agent/类别就只看相关的），再按下面的标准挑，**一次 1~5 条**：
- 优先：复现多次（`occurrences>1`）、高影响、证据具体、改动小的。
- 必须**亲自核实证据**：打开 evidence 里的事件、文件、打回意见，确认问题真实存在、且当前代码/prompt 里还没被修掉。证据对不上或已修复的，标 `dismissed` 并写原因。
- 跳过：只是口味偏好、会扩大 agent 职责边界、或与现有约定（`coordinator/prompt.md`、CLAUDE.md）冲突的。
- 涉及框架行为变更（`agent_team/` 调度、超时、信号协议）或会改变多个流程行为的，属于较大改动。

把挑选结果用表格给用户看（id、标题、要改的文件、改法一句话、你核实到的证据），**等用户确认再动手**；用户说「直接改」就不必等。

## 3. 落地
逐条改，改法以建议的 `proposal` 为起点，但以你核实的实际情况为准：
- **plan 模板**（`coordinator/plans/*.json`）：改 goal/expect/超时/依赖时，同步检查它的 `routing` 块；改完 `uv run team plan <文件> --show-vars` 确认能解析，必要时 `uv run team plan <文件> --as evolve-check` 载入后用 `uv run team tick --job evolve-check --dry-run` 验证，再 `uv run team job remove evolve-check`。
- **agent prompt / manifest**（`agents/<name>/prompt.md`、`agent.yaml`）：只改与问题相关的段落。通用交付协议（outbox 文件名、JSON 格式、产物目录、等人介入）不在 prompt.md 里，问题出在这层就改 `agent_team/worker_protocol.md`（属于框架改动，影响所有 agent）。
- **路由**（`coordinator/routing.md` 或模板 `routing` 块）、**协议文档**（`coordinator/prompt.md`、skills）：保持单一事实源，别在多处重复同一规则。
- **框架代码**（`agent_team/`）：按项目约定改完必须跑 `uv run --extra dev pytest` 和 `uv run --extra dev ruff check`，全绿才算落地；行为变更补测试。
- 每条改完立刻标记：`uv run team insights mark applied <id> --note "改了什么（文件）"`；决定不采纳的：`uv run team insights mark dismissed <id> --note "原因"`。

已采纳的建议如果之后复盘又报出来，会被自动重新打开（说明没治好），届时重新分析根因，别重复同一个改法。

## 4. 汇报
用自然语言说清：采纳了哪几条、各改了哪些文件、验证结果（测试/ruff/dry-run 输出），驳回了哪些及原因，池里还剩多少 open。建议池为空时，先 `uv run team retro status` 看是不是开关没开。改动不提交 git，除非用户要求。

## 护栏
- 不删历史 Job、产物、复盘记录；不改 `coordinator/state/` 下的运行时状态。
- 只改建议涉及的文件，不顺手重构无关代码。
- 推送、删除等高风险动作先确认。
