---
name: agent-create
description: 对话式为 agent-team 框架新建单个 agent（写 agent.yaml + prompt.md 并登记进 team.yaml）。当用户说"建个agent/新建一个agent/加个agent/创建一个 XX 角色的 agent/给团队加个成员"，或需要一个新能力的 worker 却没有匹配 agent 时使用。只建单个 agent、不编排 DAG（要连团队带流程一起搭用 team-build）。仅在 TEAMS(agent_team)项目内适用。
---

# agent-create —— 对话式新建单个 agent

把用户想要的"一个角色"，落成一个可被调度的 agent：`agents/<name>/`（含 `agent.yaml` + `prompt.md` + `workspace/`）并登记进 `team.yaml`。**只负责建 agent 这一件事**；要同时拆任务、写 plan、编排 DAG，请用 `team-build`。

## 0. 前置检查
确认当前在 agent-team 项目：存在 `team.yaml` 与 `agent_team/` 包；否则告知用户本 skill 只在该项目内可用并停止。
先 `uv run team agent list` 看已有 agent——**能复用就别新建**（能力标签能覆盖需求就直接用现有的）。

## 0.5 搜索确认真实信息（写进 agent 前先核实，绝不臆断）
往 `prompt.md` / `workspace/CLAUDE.md` / 预置的 index 里写任何**具体信息**前，先用搜索/读码/看文件把它**确认成事实**——凡是能被验证的，就不要凭记忆或想当然写：

1. **用户提到的本地路径/项目/数据源** → 先在磁盘上找到并核实再落地。
   - 路径存在性、真实位置（同名目录可能有多个，`find` 出来让用户确认或挑真实那个）。
   - 要"把某目录下的项目都加进去"时，**实际列目录**拿到真实清单；描述/定位从各项目的 `pom.xml`/`README`/模块说明等**事实来源**提取，取不到就标「待收集」，**不编造**。
2. **要在 agent 行为里用到框架机制**（人工兜底/询问、打回 reject、产物继承、awaiting 等）→ 先在 `agent_team/` 代码里**确认该机制真实存在且用法正确**，再写进 prompt/CLAUDE.md。
   - 例：人工介入靠 worker 打印行首 `[[NEEDS-INPUT]] <问题>` 触发 `AWAITING_INPUT`，协调者用 `answer` 命令注入答复（见 `pane_scan.py` / `interventions.py` / `cli.py`）。**别发明框架不支持的协议**。
3. **要沿用团队约定/布局**（skill 放哪、`skills` 字段怎么写、workspace 结构）→ 先看现有 agent 是怎么做的并对齐，而不是猜。
   - **skill 放 agent 目录级 `agents/<name>/.claude/skills/`**（不是 `workspace/` 里）：与 workspace 运行时数据分离，worker cwd 在 `workspace/<goal_id>__<task_id>/`，靠向上查找发现祖先目录的 skill，故放 agent 目录一样能被发现。装 skill 用 `cd agents/<name> && npx skills add … -y`（不加 `-g`），落到 `agents/<name>/.claude/skills/`。
   - 例：worker 的 cwd 是 `workspace/<goal_id>__<task_id>/` 独占临时目录，基座 `workspace/` 默认只读、靠向上查找继承；**要跨轮持久积累的知识库/日志/index 必须落在基座 workspace**（如 `workspace/CLAUDE.md`、`workspace/<kb>/`），并在 prompt/CLAUDE.md 里点明"基座 = cwd 上一级"，这样 goal 明确授权后 worker 才会写对地方（见 `coordinator/prompt.md` 护栏与 `coordinator.build_bootstrap`）。
4. 核实不了、或用户口径与磁盘/代码不符 → **先问用户**，别把没证实的信息固化进 agent。

> 一句话：agent 的 prompt/CLAUDE.md 是要发给 worker 当"事实依据"的，写进去前自己先当场验证一遍。

## 1. 澄清（一次问清，缺什么问什么，别挤牙膏）
建一个 agent 只需三样，其余给默认：
- **name**：合法短名（小写字母开头，`[a-z0-9_-]`，如 `api-dev`、`qa`）。用户没给就按角色拟一个并说明。
- **role**：一句话角色（如"后端接口开发""前端页面""测试""文档"）。
- **capabilities**：1–3 个**能力标签**（小写、简短：`api` `ui` `test` `doc` `review`…）。这是任务匹配的**唯一依据**——plan 里任务的 `capabilities` 要能在此命中。
可选：`max_concurrent`（该 agent 并发上限，默认 1；同名任务要真并行才调高）、`model`、`skills`。

不确定能力标签时，跟用户确认用词（标签要和将来 plan 里任务的 `capabilities` 对齐，最好复用团队已有标签的命名习惯）。

## 2. 建 agent
```bash
uv run team agent create <name> --role "<角色>" --capabilities <a,b> [--max-concurrent N]
```
这会生成 `agents/<name>/{agent.yaml, prompt.md, workspace/}` 并把 `<name>` 追加进 `team.yaml` 的 `agents:`（**会重写 team.yaml、丢注释，值不变**）。

## 3. 按需精修 manifest 与 prompt
- `agents/<name>/agent.yaml`：`capabilities`（匹配键）、`max_concurrent`、`runtime.model`（留空则用 team 默认）、`skills`（**告知**协调者/看板该 agent 有哪些 skill，不强制）、`hidden_skills`（**强制**：该 agent 的 worker 要隐藏的 skill 名，分派时并集到 `team.yaml` 的 `defaults.worker_hidden_skills` 之上；默认已填协调者专用的 `team-build`/`team-run`/`agent-create`）。
- `agents/<name>/prompt.md`：该 agent 的角色设定与工作习惯——**它会作为引导语发给 worker**，写清楚"你是谁、做什么、完成后写 outbox"。默认模板已含完成协议，按角色补充专业约束即可。
- prompt/CLAUDE.md 里凡涉及**具体路径、框架机制、团队约定**的内容，一律按 §0.5 先核实成事实再写。

## 4. 自检
```bash
uv run team agent show <name>           # 确认 role / capabilities / model
uv run team agent list                  # 确认已登记进 team.yaml
```
注意：`agent show` **不回显 `max_concurrent`**——要确认并发上限，直接看 `agents/<name>/agent.yaml`。
确认能力标签拼写与团队约定一致（大小写、连字符）；这是将来任务能否匹配到它的关键。

## 5. 交付
告诉用户：agent 已建好、能力标签是什么、下一步可用 `team-build` 把它编进某个 plan 的任务，或直接在 plan 的任务里用这些 `capabilities` 点名它这类角色。

## 护栏
- 能复用的 agent 不新建。
- **不臆造事实**：写进 prompt/CLAUDE.md/预置 index 的路径、项目清单、框架机制、团队约定，都按 §0.5 先搜索/读码/看文件核实；核实不了就问用户，别固化没证实的信息。
- name 必须合法且唯一；已存在会报错（改名或确认是否要改现有 agent）。
- 建/删 agent 都会重写 `team.yaml`（丢注释、值不变），提交前扫一眼 diff。
- 删除用 `uv run team agent remove <name>`：默认**只从 team.yaml 注销、保留 `agents/<name>/` 目录**；加 `--purge` 才连目录带信号/产物一并删除（高风险，先与用户确认）。
- 不臆造角色职责；prompt.md 里只写用户认可的设定。
