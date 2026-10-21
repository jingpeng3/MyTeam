---
name: team-build
description: 通过对话为 agent-team 框架创建 agent 与编排任务流程(DAG)。当用户想新建/编排一个多 agent 团队、把一个目标拆成任务流水线、给了一套想法要落成团队、说"建个团队/建个工作流/加个 agent/帮我编排"，或要把某需求交给团队执行时使用。仅在 TEAMS(agent_team)项目内适用。
---

# team-build —— 对话式搭建 agent 团队与工作流

把用户的想法，变成一支可运行的 agent 团队 + 一份任务 DAG。你是协调者，负责规划、落地文件、干跑验证。

**职责边界**：team-build 只负责"搭"——规划、建 agent、写 plan、dry-run 验证。**真跑执行交给 `team-run`**；**只建单个 agent（不编排 DAG）交给 `agent-create`**。搭完 dry-run 通过即算交付。

**交互主线（务必按序走）**：澄清(§1) → 出流程图+节点表(§2) → skill 推荐确认(§3) → 迭代确认(§4) → 落地(§5)。**§1–§4 是纯规划与确认，不碰任何文件、不装任何 skill**；只有用户在 §4 明确点头后才进 §5 动手。

## 0. 前置检查
确认当前在 agent-team 项目：存在 `team.yaml` 与 `agent_team/` 包。否则告知用户本 skill 只在该项目内可用并停止。
先 `team agent list` 看已有 agent——**能复用就别新建**（能力标签能覆盖需求就直接用现有的）。

## 1. 澄清需求
读用户给的想法，结合 TEAMS 的 DAG 编排方式，澄清到"能自己规划出完整 DAG"（看得出目标、环节、每步谁做、产物怎么流）为止。
- **信息够丰富**：只做轻澄清、回述确认关键点，直接进 §2 规划——别对已说清的逐条追问。
- **太薄 / 自相矛盾**：分轮澄清补齐，每轮聚焦一件事、回述确认（"我理解是…，对吗？"）再进下一轮；**用户已说清的跳过，只追问真模糊的点**。焦点顺序：
  1. 目标与产物：要达成什么？最终交付物长什么样？
  2. 环节拆解：分几步？每步大概谁做（映射能力）？
  3. 依赖与并行：哪些必须串行、哪些能并行？
  4. 复用与产物流转：复用哪些已有 agent？下游哪步要看上游哪步的产出？
- 补到能画出完整 DAG（每节点 goal/capabilities/needs 清楚、产物流转清楚）就停，进 §2。

## 2. 规划蓝图，产出「流程图 + 节点表」
先按 2.1 / 2.2 的规则把想法规划成一张 DAG + 每个 agent 的上下文包，再用 2.3 的可视形式呈现给用户确认。

### 2.1 规划 DAG（并行 / 依赖 / 产物流转）
从想法里抽出环节，自己定：
- **拆节点**：`task_id` 用描述任务内容的英文短横线命名（`crawl-users`、`build-report`、`merge-html`），别用 `t1`/`a`/`1` 之类序号——它会进看板、信号文件名、日志。
- **定依赖 `needs` 与并行**：只连真正的数据/顺序依赖；**互相没有 needs 关系的节点天然并行**（框架一轮 tick 会并发派出所有 ready 节点）。别为了"看起来有序"加多余的边，那会白白串行化。
- **静态并行 vs 动态并行（fan-out）——重要判断**：并行分支数若**设计期已知/固定**（就是要 3 个来源、5 个 TAB），直接手写多个兄弟节点，直观、蓝图清晰。若并行宽度**取决于上游运行期产出、搭建时不知道**（抓到多少项目才分几片、查到多少条 case 才并行处理），**别硬写固定份数的兄弟节点**——把该节点声明成 **fan-out 模板**（加 `fanout: {"max_parallel": N}`），运行期由协调者读上游产出决定开几路、调 `expand` 动态展开。`max_parallel` 是**同时并发上限**（宽度不限、只限并发）。判据一句话：**份数写死在设计里→静态；份数由数据决定→fan-out**。
- **每节点标 `capabilities`**：映射到某个 agent 的能力标签。
- **产物流转**：想清谁写产物、下游哪步要读上游哪步的产出——这决定后面 `see_artifacts` 怎么连。
- **评审/返工回路（可选）**：若某步产出需要另一步把关、不合格要返工，给评审/校验节点标 `reject_to`（列它可打回的上游，须 ⊆ 其祖先）。运行时该节点判上游有问题即可打回：从该上游起连同后代重跑、注入打回意见为凭证。别对每步都加评审——只在产出质量确需闸门时用。

### 2.2 为每个 agent 设计"上下文包"
worker 是独立 CLI 会话，**启动在自己的 `agents/<name>/workspace/`、只读该目录、不向上找**。所以它要带的一切都得落在自己的 workspace / manifest 里。对每个角色，设计并落地这四样：

- **能力标签 `capabilities`**（1–3 个，小写简短，如 `api` `ui` `test` `doc`）：任务匹配的唯一依据；plan 里任务的 `capabilities` 必须能被某 agent 命中，否则该任务进 `waiting_for_agent` 卡住。
- **提示词 `agents/<name>/prompt.md`**：角色人格 + 工作习惯 + 完成协议（读 inbox→执行→写 outbox）。bootstrap 会显式让 worker 读它，是**保底**通道。
- **热记忆 `agents/<name>/workspace/CLAUDE.md`**：该 agent **每次启动都自动带上**的常驻上下文——领域知识、术语、项目规范里它必须守的部分、易踩坑。worker 只读自己 workspace，所以**共享规范也要内联进来**（别指望向上查找或 import 到项目根）。这份由你按角色现写。
- **skills `agents/<name>/.claude/skills/<skill>/`**：该角色要用的专业技能，放**这个 agent 目录级 `.claude/skills/`**（与 `workspace/` 运行时数据分离）。worker cwd 在 `workspace/<goal_id>__<task_id>/`，靠向上查找发现祖先目录的 skill，故放 agent 目录一样启动即能看到描述、自动匹配或 `/name` 触发。
  - **本地优先**：先看已装 skill（`~/.claude/skills`、项目 `.claude/skills`）够不够；据此填节点表的"使用的 skill"列，本地无匹配又需专业能力的填"待推荐"（交给 §3 决定）。
  - **远端推荐走 §3 的 find-skills 闸门**（由用户决定是否查装）；**安装机制见 §5 与护栏**（直接装进目标 agent、绝不 `-g`）。
  - **依赖 skill 一起带**：若该 skill 依赖别的 skill（如 `ai-coding-daily-digest` 依赖 `date-calculator`），把依赖一并在同一 agent 目录下装进 `.claude/skills/`；落位后在 `agent.yaml` 的 `skills` 列上主 skill 与依赖，保持声明与落位一致。
- **`runtime.model` / `max_concurrent`**：按任务重量给合理默认（`max_concurrent` 是**单 Job 内**单 agent 并发上限，按 goal_id 独立结算）。

### 2.3 呈现：流程图 + 节点表
把规划结果给用户看，给两样东西（这一步只呈现、不写文件）：

1. **流程图**：用 ASCII 或 mermaid 画出 DAG——节点、依赖边、并行分支、产物流向（谁 `see_artifacts` 谁、有无 `reject_to` 回路）。让用户一眼看清串并行结构。**fan-out 模板节点画成叠层记号 `节点 ×N（≤max_parallel 并发）`**，标注"运行期按上游产出动态展开"——和看板蓝图的渲染语义对齐（看板里 fan-out 节点也画成背后错位叠层卡 + ⑃×N 徽标）。
2. **节点表**：每个节点 / agent 一行，至少三列：

   | 名字 | 功能描述 | 使用的 skill |
   |---|---|---|
   | <task_id / agent 名> | 这步干什么、吃什么、产出什么 | 拟用 skill；无专业 skill 写"内置工具"；本地无匹配待查的写"待推荐" |

若走了 §1 澄清或对薄输入做了合理假设，**在此一并标出假设点**让用户修正。

## 3. skill 推荐确认
节点表出来后，明确问用户：**是否需要让 agent 去推荐 skill？**
- **用户要**：对表里"待推荐"或本地无匹配、又确需专业能力的 agent，用 **`find-skills`** 按该 agent 职责查远端（`npx skills find <query>`），预览候选并**校验 install 数 / 来源信誉**后，把 Top 候选连同用途、装法给用户挑。
- **用户不要**：用本地已装 skill 或内置工具即可，跳过远端查找。
- 若 find-skills 也没有合适的，可提议用 **`skill-creator`** 为该 agent 现建一个专属 skill（同样落到该 agent 的 `.claude/skills/`，别落项目根共享目录）。
- 用户选定后更新节点表"使用的 skill"列（把"待推荐"替换成实际选择）。**真正安装放到 §5 落地时做，且装前必再确认**。

## 4. 迭代确认
把（可能已更新的）流程图 + 节点表再给用户，问：**还有要优化的地方吗？**
- **用户提修改** → 按反馈调 DAG / 节点分工 / skill 分配，回 §2.3 重新呈现，再问一次。**如此循环，直到用户明确确认**。
- **用户明确确认**（"可以了 / 就这样 / 开始吧"）→ 才进 §5 落地。

这是硬闸门：没拿到用户明确点头，不建 agent、不写 plan、不装 skill。宁可多确认一轮，也别拿没定稿的蓝图去落地。

## 5. 落地
按 §4 确认后的蓝图批量执行：
1. **建 / 复用 agent**：
   ```bash
   team agent create <name> --role <角色> --capabilities <a,b>
   ```
   然后写 `agents/<name>/prompt.md`（人格+协议）、`agents/<name>/workspace/CLAUDE.md`（热记忆+内联规范）；按 §3 定的 skill 装配**（连同依赖）——在目标 agent 目录下执行 `npx skills add … -y`（不加 `-g`），直接落到 `agents/<name>/.claude/skills/`，绝不装全局**；调 `agent.yaml` 的 `runtime.model`/`max_concurrent`/`skills`/`hidden_skills`（`hidden_skills` 强制对该 agent 的 worker 隐藏指定 skill，并集到 `team.yaml` 的 `defaults.worker_hidden_skills`；`agent create` 默认已填协调者专用的三个）。
2. **写 plan（任务 DAG）**：一个 plan = 一个 **Job**（按 `goal_id` 独立管理，可多 Job 并存、状态与产物互相隔离）。写到 `coordinator/plans/<goal-id>.json`：
```json
{
  "goal_id": "唯一短名",
  "goal": "一句话目标",
  "tasks": [
    {"task_id": "crawl-users", "goal": "采集用户", "capabilities": ["api"], "needs": []},
    {"task_id": "dedupe", "goal": "去重", "capabilities": ["api"], "needs": ["crawl-users"]},
    {"task_id": "build-report", "goal": "出报告", "capabilities": ["doc"],
     "needs": ["crawl-users", "dedupe"], "see_artifacts": ["dedupe"],
     "context_refs": ["shared/artifacts/spec.md"], "expect": "干完写 outbox", "max_retries": 2},
    {"task_id": "review-report", "goal": "评审报告，有问题打回", "capabilities": ["review"],
     "needs": ["build-report"], "see_artifacts": ["build-report"],
     "reject_to": ["build-report"], "max_rejects": 2}
  ]
}
```
   字段：`capabilities`(匹配依据) · `needs`(依赖 task_id) · `agent`(可选,点名写死) · `max_retries`(默认1) · `context_refs`(传给 worker 的**静态**上下文路径) · `see_artifacts`(**选择性继承上游产出文件**:列出要读其产物的上游 task_id,**必须 ⊆ 自身 needs**;框架分派时把这些节点产物绝对路径注入信号 `inherited_artifacts` 供 worker 按需读;空=不继承) · `reject_to`(**打回目标**:评审/校验类节点可打回的上游 task_id 列表,**必须 ⊆ 该节点的传递上游/祖先**;声明后 worker 判定上游有问题可在 outbox 写 `status=reject` 触发从该上游起连同后代重跑,并注入打回意见为凭证;空=不能打回) · `max_rejects`(该节点作打回目标时的累计被打回上限,0=全局默认2,超限升级给协调者裁决) · `fanout`(**动态并行模板**:`{"max_parallel": N, "shard_var": "shard", "total_var": "total"}`;声明后本节点不直接派发,上游完成即转 `awaiting_expansion` 等协调者读上游产出决定 K 后 `expand` 展开成 `<id>.1..K` 子任务;`max_parallel`=同时并发上限;goal 里用 `{{shard}}`/`{{total}}` 表示片号/总数;空=普通节点)。

#### fan-out（动态并行）节点写法
并行宽度由运行期数据决定时这样写——模板节点声明 `fanout`、goal 用分片槽，下游 join 节点先依赖模板 id（框架在 `expand` 时自动改接到实际子任务）：
```json
{
  "tasks": [
    {"task_id": "fetch", "goal": "抓清单，summary 报抓到几个", "capabilities": ["research"], "needs": []},
    {"task_id": "extract", "goal": "分片 {{shard}}/{{total}}：只处理 (rank-1)%{{total}}=={{shard}}-1 的项",
     "capabilities": ["research"], "needs": ["fetch"], "see_artifacts": ["fetch"],
     "fanout": {"max_parallel": 4}},
    {"task_id": "merge", "goal": "合并所有分片产出", "capabilities": ["html"],
     "needs": ["extract"], "see_artifacts": ["extract"]}
  ]
}
```
运行时：`fetch` 完成→`extract` 转 `awaiting_expansion`→协调者读 `fetch` summary（如「37 个」）→`team expand <gid> extract --count 12`→生成 `extract.1..12`、`merge.needs` 自动改接到全部 12 个子任务、`extract` 置 `bypassed` 容器；12 个子任务同组、同时最多 4 个在跑。
   产物：worker cwd 是各自 `workspace/`,跨 agent 产物走 `shared/artifacts/`;goal/`context_refs` 里直接写 `shared/artifacts/<文件>`,框架按 Job 自动命名空间到 `shared/artifacts/<goal_id>/`。
   **自检**（逐条过）：① 每个 `capabilities` 至少被一个 agent 命中；② `needs` 里 id 都存在、整图无环；③ 每个 `see_artifacts` 里的 id 都在**同任务的 needs** 里（否则载入期报错）；④ 每个 `reject_to` 里的 id 都是该节点的**传递上游/祖先**（否则载入期报错）——只能打回你消费过其产出的上游；⑤ 每个 `fanout` 节点的 goal **必须含 `{{shard}}`/`{{total}}` 槽**（否则各子任务 goal 完全一样、无法分工），且下游 join 节点 `needs` 先写该模板 id（`expand` 时自动重接线）。

### 5b. 可复用模板（骨架同、目标不同时）
若"流程/中间节点差不多、只是每次目标不同",写成**模板**:DAG 骨架固定,把各阶段 goal 里随目标变化的部分留成 `{{槽}}`(如 `{{objective}}`)。
```bash
team plan coordinator/plans/<模板>.json --show-vars             # 看需要哪些槽
team plan coordinator/plans/<模板>.json --as <批次id> --vars-file /tmp/vars.json
```
`--as` 覆写 goal_id → 同模板可**并发**多实例、互不覆盖;短值也可 `--var K=V`;**漏填会被拒**(残留 `{{}}` 直接报错)。

**必给模板加 `routing` 块（协调者选模板的单一事实源）**：模板顶层写一个 `routing` 对象，让协调者拿到一句话目标时能按 `coordinator/routing.md` 路由到它。纯声明、被调度引擎忽略、不进 DAG：
```json
{
  "goal_id": "case-resolve",
  "routing": {
    "when": "一句话说清适用场景（这个流程解决什么问题）",
    "signals": ["意图关键词", "用户可能怎么说", "case", "排查"],
    "not_when": "排除边界：什么情况别选它（防被更宽/更窄的目标误命中）",
    "required_slots": ["命中后要向用户收集的槽名", "…"]
  },
  "goal": "…", "tasks": [ … ]
}
```
写好后可让协调者按 `routing.md` 试跑一遍路由，确认该模板对典型目标能被命中、且 `not_when` 把不该命中的目标挡住。

## 6. 干跑验证（务必先 dry-run）
```bash
team plan coordinator/plans/<goal-id>.json            # 载入为 Job;载入期即校验悬空依赖/环/see_artifacts
team status                                           # 看该 Job 看板
team run --dry-run             # 或 tick --dry-run 逐轮看
```
确认:ready/dispatched 推进符合依赖;`waiting_for_agent` 为空(否则是能力没对齐,回 §2 修蓝图、§3 补 skill)。
**含 fan-out 模板时**:dry-run 里模板节点上游一完成就停在 `awaiting_expansion`（没有真实上游产出可读、协调者不介入展开），这是**预期**、不是卡死；真正的展开在 `team-run` 里由协调者读上游产出后 `expand`。dry-run 只需确认模板前的依赖链推进正常、模板如期转 `awaiting_expansion`。
(可选)想直观看 DAG/蓝图:`team dashboard` 起本机实时看板。

## 7. 交付与交接执行
dry-run 通过即算搭好。**不要在 team-build 里真跑**——真跑是 `team-run` 的职责。告诉用户:

> 要执行就用 **team-run**(说一句"跑起来/执行这个流程"即可)。它默认**逐轮 `tick`→`monitor` 驱动、每轮汇报**,不默认 `run` 一把梭——这样中途失败/超时/卡死当轮即可见并即时汇报。

## 护栏
- **§1–§4 不碰文件/不装 skill**：澄清与规划阶段只对话与呈现；没拿到 §4 的用户明确确认，不建 agent、不写 plan、不装 skill。
- 能复用的 agent 不新建;建/删 agent 会重写 `team.yaml`(丢注释、值不变)。
- **装 skill 前必确认**(改环境、外向动作)；**直接装进目标 agent 的 `.claude/skills/`——在该 agent 目录下执行 `npx skills add … -y`、绝不加 `-g`（连依赖 skill 一起，一律不装全局）**；删 agent `--purge`、真跑等高风险动作先确认。
- 不臆造硬需求;对薄输入做的假设要在节点表里显式标出让用户修正,别当成既定事实。
- 拆 DAG 只画主链路,别过度拆分;别加多余依赖边把可并行的节点串起来。
- 已存在的 plan 同名会覆盖,起名前先看 `coordinator/plans/`。


