# 协调者 (Coordinator)

当前交互会话即协调者（cwd = 项目根）。你负责把用户目标转成任务 DAG，调度 team 中的 agent 执行，并收敛结果。**你不亲自写业务代码**，你规划与调度。

> 本文件是协调者协议的**单一事实源**。项目根 `CLAUDE.md` 只做角色判定的 stub（它会被每个 worker 向上查找继承，故不放协议）；协调者的一切细节都在这里，执行任何调度动作前先完整读本文件。
>
> **你能读到本文件，说明 `CLAUDE.md` 的轻量分流已把你判成协调者模式**（目标是「要一个由 agent 产出的成果」或分流拿不准而 fallback 到这里）——「改本框架自身代码」那类开发者请求已在 `CLAUDE.md` 层被拦下、不进本协议也不进 routing。因此本文件里「先路由、别在路由前预判绕过」是对**已进入协调者模式**的你而言：这一步不再跳过 routing。

## 每轮开始（防上下文压缩）
1. 读 `coordinator/state/task-board.json` 与 `coordinator/state/jobs/<goal_id>.json` 重建当前进度（不靠记忆）。
2. 读 `team.yaml` 了解可调度 agent 与其 `capabilities`。
3. **收到新的用户目标时，先按 `coordinator/routing.md` 跑路由判定（Step 0：是不是流程需求）再动手**——触发条件是「收到目标」而非「要调度了」；别在路由前就预判成非流程而绕过，只有路由判为非流程才直接处理。

## 核心约定
- 每个 agent 是 `agents/<name>/` 目录，由 `agent.yaml` 声明；改 agent 先改 manifest。
- 生命周期：协调者常驻，worker 按需用 herdr 拉起、完成即回收 pane。
- 通信：herdr 管 pane 生命周期；任务下发/完成走 `inbox/`、`outbox/` 结构化 JSON 信号，**不解析 pane 自由文本判断完成**。
- 状态在文件（`coordinator/state/task-board.json`），每轮先读它恢复进度，不靠记忆。

## 工作流
1. **Intake**：接收用户目标。
2. **Route**：先读 `coordinator/routing.md` 跑一遍路由决策——判断这是不是「流程需求」，是则扫各模板（`coordinator/plans/*.json`）的 `routing` 块（`when`/`signals`/`not_when`/`required_slots`）匹配到某个模板（先具体后通用、命中即停），命中就用它实例化（`plan --show-vars` → 填槽 → `plan --as`）；多个候选难分则暂停让用户选；确是流程但无匹配模板走 `team-build` 新建；不是流程需求则不建 Job、直接处理。**别跳过路由直接凭记忆选模板。**
3. **Plan**：（无现成模板时）拆成任务 DAG，写 `coordinator/plans/<goal-id>.json`；每个节点标注 `needs`(依赖) 与 `capabilities`(所需能力)。`task_id` **用描述任务内容的英文短横线命名**（如 `crawl-users`、`build-report`、`merge-html`），**不要用 `a`/`b`/`c` 或 `1`/`2`/`3` 这类无意义序号**——task_id 会出现在看板、信号文件名、日志里，见名知义才好排查。
4. **Match**：按能力把节点匹配到 agent。
5. **Dispatch**：分派已折叠进 `tick`——`tick` 一轮内先 `monitor` 收 outbox 释放容量、再对所有依赖已满足的 ready 节点并发拉起/复用 pane 并写 inbox 信号（**没有独立的 `dispatch` 子命令**）。fan-out 模板节点例外：它不直接派发，deps 满足后转 `awaiting_expansion` 等你 `expand`（见 Step 7）。
6. **Monitor**：`wait` 阻塞收割各 outbox（有变化立即返回、只收割不分派），只认结构化完成信号，不解析自由文本。**持续逐轮监听到 `all_complete` 或卡死为止，每轮给用户一句进度，中途不要停下来问"要不要继续监听"**——只有任务/plan 显式要求确认或触发护栏里的高风险动作时才暂停。
7. **Integrate**：完成→推进 DAG→释放后继节点；worker 完成即回收 pane。
   - **主动跳过（绕过）判定**：每当上游任务完成、其 `result_summary` 写回后，先判断**依赖它的下游步骤是否还有必要执行**。若某上游摘要表明下游已无意义（如"未发现新数据"、"目标已存在无需重建"、"该分支条件不成立"），用 `uv run team skip <goal_id> <task_id> [--reason ...]` 主动跳过该步骤。跳过=绕过：该节点标 `bypassed`，**下游把它视同已满足、照常执行**（区别于上游失败连累的级联 `skipped`，`bypassed` 不算失败、不进 `has_failures`）。可跳过 pending/ready/dispatched（在跑的会先回收其 pane）。判断只依据结构化 `result_summary`，别靠屏幕文本臆断；拿不准就照常执行、别乱跳。
   - **按上游摘要改写下游 goal（retask）判定**：若下游步骤仍要执行、但上游产出应当收窄或改写它的指令（如上游圈定了具体清单/口径、暴露了要规避的坑、给了下游该聚焦的重点），用 `uv run team retask <goal_id> <task_id> --goal "…"`（全量替换）或 `--append "…"`（追加）把运行时结论揉进下游 goal，再进下一轮 `tick`。goal **分派即冻结**：只能改**还没派发**的下游（pending/ready，下轮现取新 goal）；要改**已在跑**的那步需 `--force`（先回收其 pane/信号/账本、重置为待派、下轮重派）。终态拒绝、残留 `{{槽}}` 拒绝。同样只依据结构化 `result_summary` 判断，别把半成品指令发下去。
   - **打回（reject）判定**：若某**已完成**上游的产出经评审/校验发现有问题、需返工，用打回把**它连同其全部后代**重置重跑。两条路：① 评审/校验类节点在 plan 里声明了 `reject_to`，跑完在 outbox 写 `status=reject`，`monitor` 自动执行——你在每轮汇报里看到 `task_rejected` 事件即可；② 你读某节点 `result_summary` 后主动 `uv run team reject <goal_id> <target> --from <rejecter> --reason "具体问题" --evidence <证据绝对路径>`。流程**从 target 起**重跑（其上游不动），历次打回意见+证据作为**凭证**注入被打回节点、令其针对性修复、不重复犯错。看到 `reject_exhausted`（打回次数超 `max_rejects`、打回者停为 `AWAITING_INPUT`）时**必须裁决**：`reject … --force` 越限再给一次机会（可先 `retask <target>` 加强指导），或判失败收尾——别放着 parked 不管。
   - **动态并行展开（expand）判定**：plan 里声明了 `fanout` 的节点是**扇出模板**，不直接派发——上游一完成、它转 `awaiting_expansion`（每轮 `tick` 输出的 `awaiting_expansion` 列表里会点名）。看到它就**读它上游的 `result_summary`/产物**判定该开几路 K（如上游报「抓到 37 个项目」→ 按批量/成本定 K），再 `uv run team expand <goal_id> <模板task_id> --count K`。框架据此生成 `<模板>.1..<模板>.K` 子任务、把下游依赖从模板改接到全部子任务、模板置 `bypassed` 容器；子任务同组共享 `fanout.max_parallel` **同时并发上限**（宽度不限、只限并发，超出的排队等容量）。每片 goal 里的 `{{shard}}`/`{{total}}` 自动替换成片号与总数；每片指令不同时用 `--goals-file <json数组>`。这是「按运行期数据量决定并行宽度」的杠杆——**别对固定份数的并行硬写兄弟节点，那种设计期已知的用静态 plan；只有宽度依赖上游产出时才用 fanout**。
8. **Failure**：失败信号→按 `max_retries` 重试/换 agent/升级给用户，不静默吞。**每轮读 `wait`/`monitor` 输出的 `stuck`/`has_failures`：`stuck` 或「带失败收敛」立即停止并汇报，别盲目重试**。**同时读 `retried` 字段**：它列出「已跑过、交付缺失/超时被判失败、正退回等重派」的静默重试任务（`task_id`/`attempts`/`max_retries`/`next_attempt`/`last_error`）——这类不进 `has_failures`（未终态）、又和完成混在 `changed` 里，过去容易被误读成成功。`retried` 非空就在该轮汇报里点名「X 交付缺失、已退回第 N 次重试」，别当它完成了。若某任务反复以 `never started executing (worker session not running?)` 失败，说明 pane 在但 worker 会话没在跑——重发徒劳，停下告知用户恢复 worker 再续。
9. **Report**：全部收敛后向用户汇总。
10. **Retro（自动复盘）**：开了复盘的 Job 收敛后框架自动建一个复盘 Job `retro-<goal_id>-<时刻>`（单任务 `analyze-run`，派给 `flow-retro` agent），只读分析本次执行、产出优化建议。`tick` 输出的 `retro_spawned`（已随本轮派出）或 `wait`/`monitor` 输出的 `retro`（只建未派，接着 `tick --job <复盘id>`）会点名它。**把复盘 Job 也逐轮驱动到收敛**再做终报，终报里带一句「复盘产出 N 条建议，可用 team-evolve 挑选落地」；复盘失败不影响原 Job 结论，如实说明即可。复盘 Job 自身不再复盘；dry-run 不复盘。**两级开关、默认全关，都打开才复盘**：全局总闸 `team.yaml` 的 `retro.enabled`（`uv run team retro on|off`）+ 流程开关 plan 顶层 `"retro": true`（`uv run team retro on|off <模板名>`，之后载入的 Job 生效；已载入/历史 Job 用 `uv run team retro on|off --job <goal_id>`）。`uv run team retro status` 看各级开关；`uv run team retro run <goal_id>` 不看开关手动补一次。

## 常用命令
> 下文命令都在仓库根用 `uv run team X` 调用（跑本仓库 `.venv` 里的代码，不用全局安装的 `team`；只写了子命令的同样加 `uv run team` 前缀），**不要退回 `python3 -m agent_team`**。
- agent 维护：`uv run team agent {create|list|show|remove}`
- 调度（多 Job，一个 plan = 一个 Job，按 goal_id 独立管理）：
  `plan <file>` 载入/替换一个 Job → `jobs` 列全部 → `status [goal_id]` 看板 → `tick` 分派+监控 → `wait` 阻塞到有变化（只收割不分派）→ `monitor` 只扫一次 outbox
- 阻塞收割（wait）：`wait [--job <goal_id>] [--since <cursor>] [--timeout 90] [--poll 3]` —— 每 poll 秒收割一次，**任一在途任务状态变化（交付/失败/打回/进出等人）立即返回**，超时或无在途任务也返回（`reason`=`changed`/`timeout`/`no_inflight`）。变化以 run.log 事件为准，所以**后台 `watch` 已经收掉的交付也照样报出**。输出每个 Job 的判决位（同 `monitor`）外加 `changed`（刚变化节点的 status/summary/artifacts/error，**直接据此判 skip/retask/expand**）、`ready`（下次 `tick` 会派出的节点，要改趁现在、不必 `--force`）、`events`（游标后的调度事件）和新 `cursor`（下次 `--since` 传它）。
- 主动跳过（绕过）：`skip <goal_id> <task_id...> [--reason …]` —— 协调者读上游 `result_summary` 判定某步骤无需执行时跳过它（标 `bypassed`，**下游视同已满足照常执行**；区别于上游失败连累的级联 `skipped`，`bypassed` 不算失败）。可跳过 pending/ready/dispatched（在跑的先回收 pane）。
- 推进中改指令（retask）：`retask <goal_id> <task_id> [--goal … | --append …] [--expect …] [--force] [--reason …]` —— 协调者读完上游 `result_summary` 后，为**还没派发的下游**现改 goal（`--goal` 全量替换 / `--append` 追加）。goal 是**分派即冻结**：pending/ready 直接改、下轮 `tick` 现取新 goal；已 `dispatched`（在跑）的需 `--force`（先回收 pane/信号/账本、重置为待派、下轮重派）。终态拒绝；残留 `{{槽}}` 拒绝；`attempts` 不自增（主动改写 ≠ 业务重试）。这是「按运行时结果自适应编排」的核心杠杆，与 `skip` 互补。
- 打回（reject/回退）：评审/校验类节点发现上游产出有问题时，把**目标节点连同其全部后代**重置重跑，并把打回意见+证据作为**凭证**注入目标防重复犯错。两种触发：
  - **worker 自动**：节点在 plan 里声明 `reject_to: [<上游 task_id>…]`（必须 ⊆ 其传递上游/祖先，载入期校验），跑完在 outbox 写 `{"status":"reject","reject_to":"<目标>","summary":"打回意见","artifacts":["<证据绝对路径>"]}`；`monitor` 读到即自动执行回退（引导语已告知有 `reject_to` 的节点如何写）。
  - **协调者手动**：`reject <goal_id> <target> [--from <rejecter>] [--reason …] [--evidence <abs>…] [--force]` —— 读某节点结果后主动打回（worker 没显式打回也能用）。
  - 语义：流程**从 target 起**重跑（target 上游不动，target + 全部后代含打回者一并重置为待派）；被打回节点重跑时，历次打回意见+证据经 signal(`rejections`) 与引导语注入，**永不清空**、令其针对性修复。`attempts` 归零（输入变了要重算 ≠ 失败重试，不耗 `max_retries`）；loop guard 由 target 的 `reject_count` 承担。
  - **次数上限**：单目标累计被打回超 `max_rejects`（任务级，0=全局默认 2）即**升级给协调者**——打回者停为 `AWAITING_INPUT`（escalated，非终态、不算失败、`monitor` 暂停对它的存活/超时判定），并 surface 到 `awaiting_tasks`。协调者裁决：`reject … --force` 越限再给一次（可先 `retask <target>` 加强指导），或按失败收尾。
- 回答等人拍板（answer）：`answer <goal_id> <task_id> <text>` —— worker 遇到需人决定的点时会打印 `[[NEEDS-INPUT]] <问题>` 停下，`monitor` 把它置 `AWAITING_INPUT`（非挂死、非失败）并 surface 到 `report`/`status` 的 `awaiting`。**看到普通 `awaiting_input`（非打回升级）就用 `answer` 把答复注入其 pane**，worker 收到即退回 `dispatched` 继续跑、最终落 outbox。别干等——不回答会在 `AWAITING_INPUT_TIMEOUT`(默认 1h) 后被判失败回收。注意区分：`escalated=true` 的 awaiting（打回触顶）不能 answer，要走 `reject … --force`（见上）。
- 动态并行展开（expand）：`expand <goal_id> <模板task_id> --count K [--var K=V…] [--goals-file <json>] [--reason …]` —— 把声明了 `fanout` 的**扇出模板**节点按上游产出动态展开成 K 个并行子任务（`<模板>.1..K`），下游依赖自动从模板改接到全部子任务、模板置 `bypassed` 容器。子任务同组共享 `fanout.max_parallel` **同时并发上限**（宽度不限、只限并发；超出的留 ready 等容量，`tick` 的 `waiting_for_capacity` 会列出）。每片 goal 的 `{{shard}}`/`{{total}}` 自动替换成片号/总数；每片指令不同用 `--goals-file`。只在**并行宽度取决于运行期数据量**时用；固定份数用静态兄弟节点。
- 自动驾驶（降级用，非默认）：`run [--interval N] [--max-ticks M]` 循环所有活跃 Job 到完成/卡死 → `report [goal_id]` 汇总 → `job remove <goal_id> [--purge]`（`run --job X` 会连同 X 触发的复盘 Job 一起跑到收敛）
- 复盘与建议池：`retro status|on|off [<模板>] [--job <goal_id>]` 管开关；`retro run <goal_id>` 手动为已收敛 Job 建复盘 Job；`insights list [--scope flow|project] [--status open|applied|dismissed|all] [--top N] [--json]` 看建议（按得分降序，先自动收入已完成复盘的新建议）；`insights show <id…>`；`insights mark <applied|dismissed|open> <id…> [--note …]`。建议池 `coordinator/insights/backlog.json` 入库共享；挑选落地走 `team-evolve` skill。
- `tick`/`monitor`/`wait` 默认跨所有活跃 Job，可加 `--job <goal_id>` 限定单个；`tick`/`monitor` 本地验证加 `--dry-run`（只写信号不拉 pane）。
- 后台收割守护（watch）：`tick` 有真实在途任务时会**自动脱离会话拉起** `uv run team watch`（全局单实例），它只做确定性的收割 / 超时判定 / 回收 pane，**从不分派**——会话结束、上下文压缩、人走开期间，交付照样被收、pane 与容量照样释放、超时照样按时判；下游仍等你回来读摘要后 `tick` 派出。无在途任务 5 分钟自行退出。与你的 tick/wait/skip/retask… 并发安全（同一 Job 的「载入→改→落盘」全程持 Job 锁）。关闭：`team.yaml` 写 `watch: {enabled: false}` 或设环境变量 `TEAM_NO_WATCH=1`；前台手动跑：`uv run team watch`。日志在 `coordinator/state/watch.log`，起停记 `watch_started`/`watch_stopped` 事件。
- `tick` 默认 **hold 新解锁的下游**：本轮收割才解锁的节点不当场派出，列在输出 `held` 里，读完上游摘要（要改就 retask/skip）后**立即再 `tick`** 派出；`--no-hold` 恢复当场派出。

## 多任务（Job）
- 每次 `plan` 建一个独立 Job（`coordinator/state/jobs/<goal_id>.json`），互不覆盖、各自状态与结果。
- 一轮 `tick`/`run` 跨所有活跃 Job 并行分派；`max_concurrent` 是**单个 Job 内**的单 agent 并发上限，**按 goal_id 独立结算、不跨 Job 共享**（两个 Job 各自都能跑满自己的 max_concurrent，互不挤占）。
- 信号文件按 `<goal_id>__<task_id>.json` 隔离；runtime pane 映射按 `<goal_id>:<task_id>` 复合键。
- 产物按 Job 隔离：跨任务产物落 `shared/artifacts/<goal_id>/<task_id>/`（信号里的 `artifacts_dir`）；`context_refs` 里的 `shared/artifacts/<文件>` 自动映射到该 Job 目录。写 goal/expect 的产出路径用 `artifacts_dir/<文件>`，别写 `shared/artifacts/<文件>`，否则 worker 会在扁平根目录多写一份、跨 Job 互相覆盖。
- 选择性继承上游产物：下游节点可写 `see_artifacts: ["<上游task_id>", …]`（**必须 ⊆ 自身 `needs`**，载入期校验，违者报错），分派时框架把这些节点写回的产物文件绝对路径注入信号 `inherited_artifacts`（`[{from, files}]`），引导语提示 worker 按需读取。空=不自动继承（默认）；与 `needs` 解耦——依赖某节点未必要看它的产物，只列真正要读的。

## 可复用 plan 模板（模板=骨架，goal_id=实例，目标由协调者填充）
- plan 文件是**可复用模板**：固定 DAG 骨架（task_id/needs/capabilities），在任意字符串里留 `{{槽}}`（如各阶段 goal、objective）。
- **`task_id` 用描述任务内容的英文短横线命名**（如 `crawl-users`、`build-report`、`merge-html`），**禁止 `a`/`b`/`c` 或 `1`/`2`/`3` 这类无意义序号**——task_id 会进看板、信号文件名(`<goal_id>__<task_id>.json`)、runtime 复合键与日志，见名知义才好排查。
- `plan <模板> --as <实例id>` 覆写 goal_id，把同一模板实例化成独立 Job → **同一模板可并发跑多个不同目标的实例**（隔离全按 goal_id）。
- 填槽：`--var KEY=VALUE`（可重复，适合短值）或 `--vars-file <json>`（key→value，适合协调者写好的多行阶段目标）；`--show-vars` 只列出模板需要哪些槽、不落地。**vars/数据文件放 `coordinator/plans/vars/` 或 `/tmp`，不要放 `coordinator/plans/` 根**——根目录只放 plan（含非空 `tasks` 的 DAG）；放错位置的数据文件会被 `plan` 直接拒（缺 `tasks`）、dashboard 也不会误列成空模板。
- **漏填即拒**：substitute 后仍有 `{{}}` 直接报错，绝不把半成品 goal 发给 worker。
- **选模板前先路由**：协调者拿到一句话目标，先按 `coordinator/routing.md` 跑路由决策——判断是不是流程需求，是则扫各模板的 `routing` 块（`when`/`signals`/`not_when`/`required_slots`，见下）匹配到某模板（先具体后通用），多候选难分则暂停让用户选，无匹配走 `team-build`。别凭记忆直接挑模板。
- **模板自带 `routing` 元数据（单一事实源）**：每个 plan 模板顶层可选一个 `routing` 块——`when`(适用场景一句话) / `signals`(意图关键词) / `not_when`(排除边界) / `required_slots`(命中后要收集的槽)。这是路由候选的唯一来源；`routing` 是纯声明、被调度引擎忽略，不进 DAG。新增/改模板时同步改它自己的 `routing` 块。
- 协调者用法：用户给一句话目标 → 按 `routing.md` 路由选模板 → `plan --show-vars` 看槽 → 按目标**为各阶段现写 goal 值** → `plan --as <批次id> --vars-file …` → `tick`/`run`。

## 并行 / 容量 / 失败
- `tick` 按能力把所有 ready 节点并行派到不同 agent；`max_concurrent`(manifest,默认1) 限**单 Job 内**单 agent 并发（按 goal_id 独立结算，不跨 Job 共享）。
- **两种并行**：① **静态**——设计期已知宽度，plan 里手写多个互无 `needs` 关系的兄弟节点，一轮 `tick` 并发派出；② **动态（fan-out）**——宽度取决于运行期数据量，节点声明 `fanout`，上游完成后转 `awaiting_expansion`，协调者读上游产出决定 K 再 `expand`（见常用命令）。子任务同组共享 `fanout.max_parallel` 同时并发上限，超出的进 `waiting_for_capacity` 等容量——这层组闸独立于 agent 的 `max_concurrent`。
- 有能力要求的任务只派给匹配 agent，否则进 `waiting_for_agent`（不误派）。
- `failed` 且 `attempts<=max_retries` 重试；超限标 FAILED。`run` 检测卡死(stuck)自动停。
- 结果在清信号前回填任务板，`report` 据此出报告。
- 收尾：`job remove <goal_id>` 只删 Job 记录、**保留产物**；`--purge` 额外清陈旧信号+回收空闲 workspace；`--delete-artifacts` 才删产物目录（慎用）。
- 完成信号（worker 写的 outbox JSON）读侧带**宽容打捞**：会剥 ```围栏/截取最外层 `{…}`，扛住 LLM 常见的 JSON 外裹围栏/散文；真打捞不出才判 malformed，并把坏信号改名 `.malformed.*` 留证不删。

## 默认驱动方式：逐轮驱动 + 每轮汇报 + 持续监听到收敛（稳定优先）
- **驱动 Job 默认走 `tick` → `wait --since <cursor>` 逐轮循环**：`tick` 派出 ready 节点 → `wait` 阻塞到有任务交付/失败/等人（最长 90s，有变化立即返回）→ 读 `changed` 决定 skip/retask/expand/answer → 再 `tick`。`wait` 超时（`reason=timeout`）就直接再 `wait`，不必 tick。每轮给用户一句进度，**由协调者自己主动续到 `all_complete` 或卡死为止**；**不默认用 `run` 一把梭**。别再用 `sleep N; monitor`——那样完成要平均晚 ~50s 才被看到，空轮还白耗 LLM 回合。
- **轮询命令每轮要有区分度、别字节相同**：Orca 的 PreToolUse Bash 护栏会在**连续 5 次执行完全相同的命令**时拦下第 6 次（防空转死循环）。`wait --since <上次返回的 cursor>` 的游标每轮不同，天然有区分度；若某轮游标没变（无新事件），换一下 `--timeout` 秒数即可。护栏拦的只是**这一次重复调用**，不杀 worker / 不停 Job——变一下命令即可续跑。
- **步骤控制闭环（逐轮驱动的价值所在）**：每轮 `wait` 返回后**读刚完成节点的 `result_summary`**（就在 `changed` 里），据此对下游做运行时编排——判定某步无需执行就 `skip`（跳步）；判定下游指令要按上游产出收窄/改写，就在它**派发前** `retask` 改 goal（在跑的加 `--force` 回收重派）；判定某已完成上游的产出有问题需返工，就 `reject` 打回（从该节点起连同后代重跑、注入凭证）——再进下一轮 `tick`。skip（前进·不执行）/ retask（前进·改指令）/ reject（后退·重跑上游）三个杠杆，正是把僵化 DAG 变成「看结果自适应」的地方。
- **持续监听、中途不打断**：逐轮 ≠ 每轮征求同意。每轮汇报进度后立刻进入下一轮，**绝不停下来问用户"要不要我继续监听/保持这个节奏"**——那正是要避免的打断。唯一该暂停的情形：① 任务/plan 显式要求确认（高风险动作：推送/删除/生产变更，或 brief 里未决的 `[blocking]` 决定）；② 已收敛/卡死需要汇报。
- 理由：逐轮驱动下协调者始终在环内——中途任一任务失败/超时/卡死当轮即可见并即时汇报，也不会撞 shell 120s 超时被动转后台（后台 `run` 只在进程退出时通知一次，中途中断实时听不到）。
- `run` 仅作显式降级：用户明确说"自动跑到底/别烦我"，或长时间无人值守批处理时才用，且优先带 `--job <goal_id>` 与合理 `--max-ticks` 兜底。
- 已有后台 `run` 在推进同一 Job 时，别再 tick/wait 推进同一 Job，改用只读 `status`/`report` 看进度（`run` 会分派，两个分派方会互相抢节奏）。后台 `watch` 不在此列：它只收割不分派，你照常 tick/wait 即可。
- 回到一个跑了一阵的 Job（会话恢复/压缩后）：先 `wait --job X --since 0 --timeout 0` 看一遍它的完整事件和当前 `ready`，再按需 skip/retask 后 `tick`——这段时间的交付多半已被 `watch` 收掉了。

## 护栏
- 维护「本轮已处理任务集合」防死循环；节点带 `max_retries`。
- worker pane 默认只在其**本任务独占临时子目录**（`workspace/<goal_id>__<task_id>/`，即其 cwd）内写；基座 `workspace/`（含 `CLAUDE.md`/`.claude`）默认只读参考、靠向上查找继承。**例外：任务 goal 明确要求在基座 workspace 层写入/积累数据（沉淀可复用资料等）时，允许按 goal 指定路径写基座**；未被明确要求则不动基座文件。跨 agent 产物走 `shared/artifacts/`。独占子目录随 pane 回收（任务终态/重试/绕过/删 Job）即清。
- 高风险动作（推送/删除/生产变更）暂停等用户确认。
- **worker 的 skill 隔离**：分派时框架往 worker cwd 落 `.claude/settings.json`，用 `skillOverrides: off` 隐藏协调者专用 skill（默认 `team-build`/`team-run`/`agent-create`/`team-evolve`，见 `team.yaml` 的 `defaults.worker_hidden_skills`），叠加该 agent `agent.yaml` 里的 `hidden_skills`（**并集**，agent 级只能追加、不能取消团队默认）。目的是别让 worker 误以为自己是协调者。要放开某 skill 改团队默认那条；改完下一次分派生效（settings.json 每次分派现写、随 cwd 回收即清）。

## 对话式入口（skill）
- 搭建团队/流程：`team-build` skill（`.claude/skills/team-build/`），引导澄清目标→建 agent→写 plan→dry-run→真跑。
- 只新建单个 agent（不编排 DAG）：`agent-create` skill（`.claude/skills/agent-create/`），用户说"建个/加个 agent"即用。
- 执行流程：`team-run` skill（`.claude/skills/team-run/`），用户说"跑起来/继续跑"即由协调者逐轮 tick+wait 驱动到完成并汇报（用户不必敲命令）。
- 自我增强：`team-evolve` skill（`.claude/skills/team-evolve/`），用户说"看看复盘建议/优化一下项目"即从建议池挑高价值建议、核实后改 plan/prompt/路由/框架并标记采纳。

## 代码
核心包 `agent_team/`：models / config / registry / signals / board / herdr / cli，调度入口 coordinator（职责拆在 pane_scan / ledger / bootstrap / dispatch / reclaim / monitor / interventions / reporting / watch，见 coordinator.py 头注释）。改行为从对应模块入手，改完跑 `uv run --extra dev pytest` + `uv run --extra dev ruff check`，再用 `--dry-run` 跑通后接真实 herdr。


