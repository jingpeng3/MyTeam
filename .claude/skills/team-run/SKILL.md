---
name: team-run
description: 驱动 agent-team 把已编排的工作流(plan/DAG)执行到完成——拉起 worker、监控 outbox、回收 pane、汇总结果。当用户说"执行/跑这个流程、开始跑、让团队干活、继续跑、推进任务、把这个 plan 跑起来"时使用。用户不必自己敲命令，由你代为驱动。仅在 TEAMS(agent_team)项目内适用。
---

# team-run —— 对话式驱动工作流执行

用户说一句"跑起来"，你就把已编排好的任务 DAG 驱动到完成。你是协调者：拉起 worker、盯 outbox、推进依赖、回收 pane、最后用人话汇报。用户不用碰命令行。

## 0. 前置检查
- 确认在 agent-team 项目（有 `team.yaml`、`agent_team/`）。下文命令都是 `team` CLI；只写了子命令的（如 `status <goal_id>`）同样加 `team` 前缀。`team` 找不到时先在仓库根执行 `scripts/install.sh` 安装，别退回 `python3 -m agent_team`。
- 确定要跑的 Job（一个 plan = 一个 Job，按 `goal_id` 独立管理，可多 Job 并存）：
  - 用户点名了 plan 文件 → `uv run team plan coordinator/plans/<name>.json` 载入为 Job（不影响其它 Job）。
  - **plan 是模板（含 `{{槽}}`）或用户给了新目标** → `plan <模板> --show-vars` 看槽，按用户目标**为各阶段现写 goal 值**，再 `plan <模板> --as <批次id> --vars-file /tmp/vars.json`（或 `--var K=V`）实例化；`--as` 让同模板可并发多实例、漏填会被拒。
  - 已有在跑的 Job → `uv run team jobs` 看全部，直接**续跑**（状态在文件，可恢复）。
  - 什么都没有 → 提示先用 `team-build` 编排，或让用户指明 plan，然后停。
- `uv run team jobs` 看有哪些 Job；`status <goal_id>` 看某个 Job 明细。

## 1. 真跑确认（重要）
真跑会**真实拉起 herdr worker，消耗 token、产生真实文件改动**。除非用户已明确要真跑，先说明并确认。
想先验证编排可加 `--dry-run`（只写信号不拉 pane）。

## 2. 执行循环（逐轮驱动 + 持续监听到收敛，跨 Job 并行，别长时间阻塞）
按「分派→轮询→推进」逐轮走，每轮给用户一句进度，**由你自己主动续到 `all_complete` 或卡死为止**，不要用一个超长阻塞调用干等，也**不要每轮停下来问用户"要不要继续监听"**：

1. **分派**：`uv run team tick`
   - 默认**跨所有活跃 Job**：把各 Job 的 ready 节点按能力并行派到 worker（fire-and-forget，秒回）。
   - 只想跑某个 Job 加 `--job <goal_id>`。
   - 读输出：每个 Job 的 `dispatched`、`waiting_for_agent`（没抢到空闲 agent 的，正常，下轮补——**容量是跨 Job 全局的**，`max_concurrent` 限单 agent 并发）。
   - `held` 非空：这些是**本轮刚收割才解锁**的下游，tick 故意没派，留给你先读上游摘要。要改就 `retask`/`skip`（此时还没派发，不用 `--force`），然后**立即再 `uv run team tick`** 把它们派出。
2. **等完成**：`uv run team wait --job <goal_id> --since <cursor>`（首轮不带 `--since`；之后传上次返回的 `cursor`）。
   - 每 3s 收割一次，**任一在途任务交付/失败/打回/进出等人就立即返回**，最长阻塞 90s（留在 shell 120s 超时内）；只收割、**不分派**。
   - `reason=changed`：读 `jobs.<goal_id>.changed`（刚变化节点的 status/summary/artifacts/error）和 `ready`（下次 tick 会派出的节点），据此决定 skip/retask/expand/answer，再回第 1 步 `tick`。变化按 run.log 事件判，后台 watch 已收掉的交付也会报出。
   - `reason=timeout`：没变化，直接再 `wait`（不必 tick）。`reason=no_inflight`：没有在途任务，回第 1 步 `tick`（或已收敛/卡死）。
   - 别再用 `sleep N; team monitor` 轮询：完成平均要晚 ~50s 才被看到，空轮白耗回合；`--since` 游标每轮不同，也不会撞 Orca 的重复命令护栏。
   - **完成判定只认 outbox 信号**（文件名按 `<goal_id>__<task_id>` 隔离），别去解析 worker 屏幕文本判断完成。
   - **每轮必读 wait 输出的 `stuck` / `complete` / `has_failures` 三个判决位**（新增）：`stuck:true`、或 `complete:true && has_failures:true` → **该 Job 已无法继续/已带失败收敛，立即停止本 Job 的循环并向用户汇报**，别再 tick 盲目重试。
   - 想看进度可 `herdr agent read <name> --source recent-unwrapped --lines 60` 瞄一眼，但别用长 `agent wait` 干等（会白等，教训）。
   - wait/monitor 会把 done 的任务收敛、**自动回收其 pane**，并解锁后继节点（含释放容量给别的 Job）。
3. **推进**：只要还没 `all_complete`、没 `stuck`、也没「带失败收敛」，**自动回到第 1 步继续下一轮**——每轮汇报一句进度即可，别把"是否继续监听/保持节奏"当问题抛给用户（持续监听是默认职责，唯一例外见下方护栏）。
   - 省事也可 `uv run team run`（自动循环所有 Job 到底），但交互中优先 tick+wait 以便实时汇报。

> **后台收割守护（watch）**：`tick` 有真实在途任务时会自动脱离会话拉起 `uv run team watch`（全局单实例）。它只收割 outbox / 判超时 / 回收 pane，**从不分派**——你中途离开、会话被压缩，交付照样被收、容量照样释放；下游仍等你回来 `tick`。它与你的命令并发安全，**不用因为它在跑就改用只读命令**。回到跑了一阵的 Job 先 `uv run team wait --job X --since 0 --timeout 0` 看全部事件和 `ready`。关闭：`team.yaml` 写 `watch: {enabled: false}` 或 `TEAM_NO_WATCH=1`。

> **主动跳过（绕过）无需执行的步骤**：每轮 `wait` 收到上游完成信号后，读它的 `result_summary`（就在 `changed` 里），判断**依赖它的下游步骤是否还有必要跑**。若摘要表明下游已无意义（如"未发现新数据/目标已存在/条件不成立"），用 `uv run team skip <goal_id> <task_id> [--reason "…"]` 跳过它。语义是**绕过**：该节点标 `bypassed`，下游把它当**已满足**照常执行（≠ 上游失败连累的级联 `skipped`；`bypassed` 不算失败、不进 `has_failures`）。可跳过 pending/ready/dispatched（在跑的会先回收 pane+清信号）。命令返回 `bypassed`/`newly_ready` 等，据此汇报"因某某，跳过 X，解锁 Y"。判断只依据结构化 `result_summary`，拿不准就照常执行、别乱跳。

> **按上游结果改写下游 goal（retask）**：若下游仍要跑、但上游产出应当收窄/改写它的指令（上游圈定了具体清单口径、暴露了要规避的坑、给了该聚焦的重点），用 `uv run team retask <goal_id> <task_id> --goal "…"`（全量替换）或 `--append "…"`（追加）把运行时结论揉进下游 goal，再进下一轮 `tick`。goal **分派即冻结**：只能改**还没派发**的下游（pending/ready，下轮现取新 goal）；改**已在跑**的那步需 `--force`（先回收 pane/信号/账本、重置为待派、下轮重派）。终态拒绝、残留 `{{槽}}` 拒绝；`attempts` 不自增（主动改写 ≠ 业务重试）。同样只依据结构化 `result_summary` 判断。

> **打回返工（reject）**：若某**已完成**上游的产出经评审/校验有问题、需返工，把**它连同其全部后代**重置重跑。两条路：① 评审/校验类节点在 plan 里声明了 `reject_to`、跑完在 outbox 写 `status=reject`，`monitor` **自动执行**——你在汇报里看到 `task_rejected` 事件即可；② 你读某节点 `result_summary` 后主动 `uv run team reject <goal_id> <target> --from <rejecter> --reason "具体问题" [--evidence <证据绝对路径>]`。流程**从 target 起**重跑（其上游不动），历次打回意见+证据作为**凭证**注入被打回节点、令其针对性修复、不重复犯错；`attempts` 归零（≠ 失败重试）。**看到 `reject_exhausted`（打回次数超 `max_rejects`、打回者停为 `AWAITING_INPUT`）必须裁决**：`reject … --force` 越限再给一次（可先 `retask <target>` 加强指导），或判失败收尾——别放着 parked 不管。skip / retask / reject 三个杠杆合起来，就是把僵化 DAG 变成「看运行时结果自适应」的地方。

> **回答等人拍板（answer）**：worker 遇到需人决定的点会打印 `[[NEEDS-INPUT]] <问题>` 停下，`monitor` 把它置 `AWAITING_INPUT`（非挂死、非失败）并 surface 到 `report`/`status` 的 `awaiting`。看到普通 awaiting_input 就用 `uv run team answer <goal_id> <task_id> "答复"` 把答复注入其 pane，worker 收到即退回 dispatched 继续跑。别干等——不回答会在默认 1h 超时后被判失败。（`escalated=true` 的 awaiting 是打回触顶，不能 answer，要走 `reject … --force`。）

> **动态并行展开（expand）**：plan 里带 `fanout` 的**扇出模板**节点，上游一完成就转 `awaiting_expansion`——`tick` 输出的 `awaiting_expansion` 列表会点名它。看到就**读它上游的 `result_summary`/产物**判定该开几路 K（按数据量/成本），再 `uv run team expand <goal_id> <模板task_id> --count K`。框架据此生成 `<模板>.1..K` 子任务、把下游依赖自动改接到全部子任务、模板置 `bypassed` 容器；子任务同组共享 `fanout.max_parallel` **同时并发上限**，超出的进 `waiting_for_capacity` 排队等容量。每片 goal 的 `{{shard}}`/`{{total}}` 自动替换；每片指令不同用 `--goals-file <json数组>`。**不展开就一直停在 `awaiting_expansion`**（非卡死、非失败，靠你推进）——每轮看到就去展开，别忽略。

> **并发是常态，别当异常**：状态是共享的，同时存在**别的 Job**、或**另一个会话/后台 `run` 循环**在推进这些 Job，都是多 Job/多会话设计内的正常情况——不要把它当成「发现未知并行会话」之类的告警抛给用户。做法：
> - 只盯**自己负责的 `goal_id`**（`status <goal_id>` / `report <goal_id>` / `tick|wait|monitor --job <goal_id>`）；别的 Job 各跑各的，互不影响（信号/产物/容量都按 goal_id 隔离）。
> - 若已有后台 `run` 循环在驱动，就**别再对同一 Job tick/wait/monitor**（会和它抢同一份 outbox）——改用**只读**的 `status`/读 board 看进度，让 run 收割即可。
> - 自己 Job 完成、产物齐全，就正常汇报收尾；不必解释、更不必去动别的会话的 Job、pane 或 `run` 进程。

## 3. 失败与卡死
- 任务 `failed` 且未超 `max_retries` → 自动回队列重试；超限 → 标 `FAILED`。
- `stuck`（无完成、无在跑、无可推进 ready）→ 停止并诊断：多半是**能力标签没对齐**（`waiting_for_agent` 长期不空）或**依赖被某个 FAILED 节点卡住**。把卡点讲清楚，给出改法（改 plan 能力/加 agent/重跑失败节点），别硬循环。
- **worker「起了 pane 但没在执行」**：monitor 里若某任务反复以 `agent idle, never started executing (worker session not running?)` 失败，说明 pane 进程在、但**背后的 agent 会话没在跑 turn**（被删/被停/backend 没起）。**重发到同一个不干活的 backend 是徒劳**——别继续 tick 重试，停下来告诉用户"worker 会话没在执行，任务无法继续"，让用户恢复 worker 后再续跑。
- **判决位**：`wait`/`monitor` 每轮输出 `stuck` / `has_failures`；`stuck:true` 或 `complete && has_failures` 就是"无法继续/带失败收敛"的明确信号，据此停止+汇报。

## 4. 收尾
- **自动复盘**：开了复盘的 Job 收敛时框架会自动建复盘 Job `retro-<goal_id>-<时刻>`（`flow-retro` agent 只读分析本次执行）。`tick` 输出的 `retro_spawned` 表示已随本轮派出；`wait`/`monitor` 输出里的 `retro.retro_spawned` 表示只建未派，接着 `uv run team tick --job <复盘id>`。照常逐轮 `wait --job <复盘id>` 到收敛（通常几分钟），终报里带一句「复盘产出 N 条建议，可说"看看复盘建议"用 team-evolve 落地」。复盘失败不影响原 Job 结论。没有 `flow-retro` agent 时不会复盘。**两级开关、默认全关，都打开才复盘**：全局总闸 `team.yaml` 的 `retro.enabled`（`uv run team retro on|off`）+ 流程开关 plan 顶层 `"retro": true`（`uv run team retro on|off <模板名>`，之后载入的 Job 生效；已载入/历史 Job 用 `uv run team retro on|off --job <goal_id>`）。`uv run team retro status` 看各级开关；`uv run team retro run <goal_id>` 不看开关手动补一次。
- `uv run team report <goal_id>` 出该 Job 终报（done/failed、每个任务的 summary 与 artifacts 路径）；省略 goal_id 则聚合所有 Job。
- 核对无残留 worker pane：`herdr pane list`；若本次自己拉起的 pane 还在（异常中断时可能发生），回收掉；不确定归属就**默认不动**（可能是别的会话/Job 的），别乱关不是自己建的 pane。
- 收尾的 Job 可 `uv run team job remove <goal_id>` 移除（只删 Job 记录、**保留产物**）；`--purge` 额外清陈旧信号+回收空闲 workspace；确需删产物再加 `--delete-artifacts`。

## 5. 汇报
用自然语言总结：目标完成没、几个任务成/败、关键产物在哪、有无需要人处理的卡点。别把原始 JSON 直接甩给用户。

## 护栏
- 真跑、重跑失败节点、关 pane 等有副作用的动作，先确认。
- 完成靠 outbox 信号，不靠屏幕文本；pane 由 monitor 回收。
- 续跑时不要重复分派已 dispatched 的任务（monitor 会照常收它们的 outbox）。
- **持续监听、中途不打断**：一旦开跑，自己逐轮续到 `all_complete`/卡死，每轮只汇报一句进度，**不要停下来问"要不要继续监听/保持节奏"**。唯一可暂停的情形：① 任务/plan 显式要求确认或高风险动作（推送/删除/生产变更、brief 里未决的 `[blocking]` 决定）；② 已收敛或卡死需要汇报。
- **别把并发当异常**：同时有别的 Job、或另一个会话/后台 `run` 在推进共享状态，是正常的；只按 `goal_id` 关注自己的 Job，别去动或上报别的会话的 Job/pane/进程。
- 高风险动作（推送/删除/生产变更）一律暂停等用户确认。
