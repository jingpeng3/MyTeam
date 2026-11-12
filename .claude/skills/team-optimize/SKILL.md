---
name: team-optimize
description: 周期性流程优化 campaign——回顾一个时间窗（默认上一个自然周）内真实跑过的全部 Job，按流程逐条优化 plan 模板与 agent prompt：每个流程一个 worktree + 一个优化 subagent，再各配一个独立评审 subagent，代码门禁通过后自动合并到本地主分支；框架层问题登记进建议池，最后生成 HTML 优化报告。当用户说"回顾上周的流程/复盘上周跑的 job 并优化""周度流程优化""按实际运行情况优化各个流程""跑一次流程优化""team optimize"时使用。仅在 TEAMS(agent_team)项目内适用。
---

# team-optimize —— 回顾一段时间的真实运行，逐流程优化、评审、合并、出报告

你是**编排者**（开发者角色，不是协调者）：不建 Job、不跑 `uv run team plan/tick/run`、不用 herdr。你负责派 subagent、看门禁、判修复轮次、合并、收尾。确定性的部分全部交给 `uv run team optimize` 子命令，**不要手算指标或手改 campaign.json**。

和相邻能力的分工：`flow-retro` 是单 Job 收敛后的只读复盘；`team-evolve` 从建议池挑几条落地；本 skill 是**一个时间窗内全部流程**的批量优化。框架问题（`agent_team/` 等）本 skill 只记录不修改，交给 `team-evolve`。

## 0. 前置
- 在项目根（有 `team.yaml`、`agent_team/`），当前分支是主分支、工作区干净（`git status --short` 为空）。有别人的未提交改动就停下问用户，不要替人提交或丢弃。
- 先跑一次 `uv run --extra dev pytest -q` 和 `uv run --extra dev ruff check` 确认基线是绿的；基线就红就停下报告，不开 campaign。
- 用户指定了时间窗就照用户的；没说就用默认（从书签续接）。

## 1. collect：收集证据、选流程
```bash
uv run team optimize bookmark                     # 书签：已覆盖到哪天、下次默认窗口、各流程最近一次优化与之后的新 Job 数、未收尾 campaign
uv run team optimize collect                      # 默认从书签续接：上次覆盖日次日 ~ 昨天（无历史时上一个自然周）；最多 10 条流程
uv run team optimize collect --days 7             # 过去 7 天；或 --since 2026-10-26 --until 2026-11-01
```
- **先看书签**：`unfinished` 非空时 collect 会拒绝新建。能续跑就按 `uv run team optimize status --campaign <id>` 接着推进到 finish；确认废弃的用 `uv run team optimize abandon --campaign <id> --reason …`（已有流程合并的不能放弃，只能 finish）。不要用 `--force` 绕过，除非用户明确要并行开新 campaign。
- 书签提示「还没有新的完整日期」时，说明上次已覆盖到昨天，告诉用户无需再跑；用户坚持重看就用 `--since/--until`。
- 某流程上次优化合并之前开始的 Job 记为 `stale_jobs`（改动前），信号不计分、指标不进基线；全是改动前 Job 的流程会跳过，等新 Job 验证效果。
输出里看：`selected`（入选流程，按问题信号得分排序）、每个流程的 `status/reason/jobs/allowed/owned_agents/requests_agents/shared_agents`、`framework_candidates`（框架信号）、`previous_effect`（上次优化的效果）、`pending_cross_flow`（以往 campaign 里未闭环的跨流程请求，带 `id/requester/owner/target_flow/status`）。证据在 `<campaign 目录>/evidence/<flow>.md`；owner/目标/发起流程入选时，其证据里有「上次未闭环的跨流程请求」小节。

- 选择规则是代码定的：窗口内已收敛、有真实执行、能匹配到 plan 模板的 Job；有流程类问题信号才入选；单次最多 10 条；临时 plan 和健康流程只进报告。**不要自己加减流程**；用户要求加某条健康流程时，说明它没有问题信号、证据不足，用户坚持再加。
- 共享 agent 的 owner 也是代码定的（窗口内 Job 最多的入选流程）。非 owner 流程不能改它，只能在 optimize.json 的 `cross_flow` 里提请求（对象：`owner/target_flow/request/evidence/status/verification`，status 取 `open|done|deferred|rejected`），由你在合并前协调。
- 把入选清单、跳过原因、框架信号用一张表告诉用户，然后**直接继续**，不必等确认（用户已授权自动合并）。没有入选流程时直接跳到第 7 步出报告。

## 2. 拉 worktree 并派优化 subagent（并行）
对每个入选流程：
```bash
uv run team optimize worktree <flow>              # .worktrees/<campaign>/<flow>，分支 opt/<campaign>/<flow>
```
然后**在同一条消息里**为全部入选流程各派一个 `general-purpose` subagent（多于 5 个就分批，每批同一条消息发出）。提示词用 `uv run team optimize prompt optimizer <flow>` 生成（由 `templates/optimizer.md` 填好 worktree、证据、白名单、共享 agent、跨流程 owner、基线 commit），原样作为 subagent 的 prompt，不要删减准则段落；需要补充上下文就追加在末尾。

subagent 交付后：
```bash
uv run team optimize record optimizer <flow>      # 记录 HEAD 与改动条数
uv run team optimize check <flow>                 # 门禁：边界、commit 前缀、optimize.json 契约、JSON/plan、diff、pytest、ruff
```
门禁不过：把 `gate-r<N>.json` 里失败项原样转给同一个 subagent（SendMessage）修，修完再 `record optimizer` + `check`。门禁问题不计入评审修复轮次，但同一流程门禁连续 3 次不过就 `uv run team optimize log abandoned --flow <flow> --field reason=…` 放弃。
门禁结果 `noop: true` 表示优化者显式声明本流程已无可改进项（`optimize.json` 写 `"noop": true` + 空 changes + summary 说明）：不要再派评审，直接 `uv run team optimize log abandoned --flow <flow> --field reason=本流程已无可改进项` 并跳过该流程。

**边界例外**：优化者确有需要改白名单外的流程层文件（例如本流程独占 agent 的 playbook 在别的目录），由你判断后 `uv run team optimize scope <flow> <路径> --reason …`。框架、测试、协议、配置、`agent.yaml` 是禁止区，命令会拒绝，也不要绕过。

## 3. 派评审 subagent（并行，独立）
门禁通过的流程，`uv run team optimize log review_started --flow <flow>`，然后各派一个**新的** `general-purpose` subagent 做评审（不能复用优化者），提示词用 `uv run team optimize prompt reviewer <flow>` 生成（轮次自动递增）。评审只读，产出 `flows/<flow>/review-r<N>.json` 与 `.md`。交付后：
```bash
uv run team optimize record review <flow>         # 校验 verdict；PASS 但仍有未修复 blocking 会被拒绝
```

## 4. 修复轮（每个流程最多 2 轮）
- `FAIL`：`uv run team optimize log fix_round --flow <flow>`，把评审的 blocking/major 项和非阻塞里低风险高价值的项一起发给**原优化者**（SendMessage），要求追加 `fix(<flow>): …` commit、更新 optimize.json。修完 `record optimizer` → `check` → 把 `uv run team optimize prompt reviewer <flow>` 生成的增量复审提示词发给**原评审员**（写 `review-r<N+1>`，只看上轮 HEAD 之后的增量）→ `record review`。
- `PASS` 但有非阻塞建议：低风险高价值的（措辞冲突、明显误差、口径矛盾）也按上面走一轮修复+复审；其余在评审文件里标 `deferred`，进报告的遗留项。
- 第 2 轮修复后仍 FAIL：`fix_round` 会被拒绝，按准则 `uv run team optimize log abandoned --flow <flow> --field reason=…` 放弃，该流程不合并，分支保留备查。

## 5. 合并（自动，本地主分支，不 push）
跨流程请求先处理：本次各流程 optimize.json 的新请求，加上 collect 输出的 `pending_cross_flow`（以往未闭环的），凡 owner 流程本次入选的，转给 owner 的优化者在其分支里做（仍走门禁和复审）。owner 处理后在自己的 optimize.json `cross_flow` 里写同一个 `id`、`status: "done"` 并填 `verification`（怎么验证已处理）；暂不做标 `deferred`、明确不做标 `rejected`，都写原因。id 从哪拿：以往的看 `pending_cross_flow`；本次新提的看发起流程 `uv run team optimize record optimizer <flow>` 输出的 `cross_flow`（每条的 `id/owner/status/request`），转给 owner 时带上。没带 id 也行：owner 照抄发起方的 `requester/owner/request` 原文（不写 id），生成的 id 相同。只有已合并流程里的请求和 done/rejected 才算数（同一 campaign 内也一样）：未闭环的会在报告「跨流程请求」小节按 id 汇总，并在下次 collect 里继续提示；发起流程没合并的请求在报告里标「未合并，不跟踪」，需要的话让它在下次重提。owner 本次没入选的，留给下次。然后：
```bash
uv run team optimize merge                        # 只合 approved 且门禁通过的流程，--no-ff 依次合入，再跑主分支全量门禁并清理 worktree
```
- 返回 `conflict` 非空：停下，把冲突流程和文件告诉用户，不自动解冲突、不 force。
- 主分支门禁失败：停下报告失败项（`main-gate.json`），不要回滚别人的提交；和用户确认后再处理。
- 永远不 push、不改远端。

## 6. 记录框架问题
优化者和评审员都会在各自的 JSON 里报 `framework_issues`。你自己在过程中发现的（例如 collect 的 `framework_candidates` 里证据充分但没人认领的）写成 JSON 数组放 `<campaign 目录>/framework-extra.json`。然后：
```bash
uv run team optimize issues --dry-run --file <campaign 目录>/framework-extra.json   # 先看将登记什么
uv run team optimize issues --file <campaign 目录>/framework-extra.json
```
条目字段与建议池一致：`title / target / problem / evidence[] / proposal / impact / effort / confidence`（`dup_of` 可指向已有 id）。同一问题会按指纹去重，跨 campaign 复现会累加 `occurrences`。每条都要有可核对的证据（run.log 行号、事件、代码 `文件:行`），没有证据不登记。

## 7. 收尾与报告
```bash
uv run team optimize finish                       # result.json + 写 coordinator/insights/optimize-history.json + report.html
git add coordinator/insights/backlog.json coordinator/insights/optimize-history.json
git commit -m "chore(optimize): <campaign> 优化历史与框架问题" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```
- 只提交这两个 insights 文件；campaign 明细与报告在 `shared/artifacts/optimize/<campaign>/`，不入库。
- `uv run team optimize report` 只重渲染报告（改了评审文件或补了数据后用）。

最后用中文给用户汇报：时间窗与 Job 数；每个流程的结论（合并/放弃/跳过）和核心改动；评审轮次与遗留项；主分支门禁结果与合并 commit；登记的框架问题 id；上次优化效果；报告路径。

## 准则（编排者必须守住）
- **证据先行**：每条改动都要能回指 job id + run.log 行号/指标/打回原文；评审要重新核对数字。
- **边界由代码校验**：门禁的 `scope` 失败一律修，不用 `scope` 命令给框架/测试开口子。
- **共享 agent 不回归**：owner 改共享 agent 时，新增行为必须条件化，评审要专门检查其他使用流程。
- **评审独立**：评审员与优化者是不同 subagent，只读；修复后必须复审。
- **软预算只为收敛**：不能截断已有内容或跳过自检。
- **日志完整**：所有状态推进都走 `uv run team optimize` 子命令（它们会写 optimize-log.jsonl）；人工判断用 `uv run team optimize log note --field msg=…` 留痕。
- **不越权**：不 push、不删历史 Job/产物、不改 `coordinator/state/`、不改框架代码；主仓有他人改动时先问。
