---
name: team-evolve
description: 从复盘建议待办池批量落地优化——先由独立分析 agent 合并同义建议并按「改动文件集互不相交」分组，每组一个 worktree + 一个开发 subagent 并行实现，每组再配一个独立评审 subagent，全部通过门禁与评审后自动 --no-ff 合并到本地 main（不 push），最后回写建议池状态并出报告。当用户说"优化一下项目/团队""看看复盘建议""采纳建议""自我进化/自我增强""根据执行情况改进流程""team insights""按建议批量改"时使用。仅在 TEAMS(agent_team)项目内适用。
---

# team-evolve —— 建议池批量落地：分析合并 → 分组并行开发/评审 → 合并 main

你是**编排者**（开发者角色，不是协调者）：不建 Job、不跑 `uv run team plan/tick/run`、不用 herdr。你负责派分析/开发/评审 subagent、看门禁、判修复轮次、合并、收尾。确定性的部分全部交给 `uv run team evolve` 子命令，**不要手算分组或手改 run.json**。

与相邻能力的分工：`flow-retro` 是单 Job 收敛后的只读复盘；`team-optimize` 是回顾一个时间窗、按流程优化 plan/prompt；本 skill 是把**建议池里的条目**批量落地——可以改框架代码、测试、协议与 skill。

## 0. 前置
- 在项目根（有 `team.yaml`、`agent_team/`），当前分支是主分支、工作区干净（`git status --short` 为空）。有别人的未提交改动就停下问用户，不要替人提交或丢弃。
- 先跑一次 `uv run --extra dev pytest -q` 和 `uv run --extra dev ruff check` 确认基线是绿的；基线就红就停下报告，不开 run。

## 1. 选题
```bash
uv run team insights list --json --top 12        # 先自动收入已完成复盘的新建议，再按得分降序
uv run team insights show <id...>                # 看某几条的 problem/proposal/evidence 全文
```
按用户意图筛（指定了流程/agent/类别就只看相关的），再按下面标准挑，**一次最多 12 条**：
- 优先：复现多次（`occurrences>1`）、高影响、证据具体、改动小的。
- 跳过：只是口味偏好、会扩大 agent 职责边界、或与现有约定（`coordinator/prompt.md`、`CLAUDE.md`）冲突的——这类留给分析阶段驳回。
- 你以为已修复或证据对不上的，先自己打开 evidence 里的文件/事件确认；不确定就保留，让分析 agent 复核。

把挑选结果用表格给用户看（id、标题、要改的文件、一句话改法、你核实到的证据），**等用户确认再开始**；用户说「直接改」就不必等。

## 2. 合并分析：独立 agent 输出分组
```bash
uv run team evolve start --ids <id1,id2,...>     # 建 run、抓基线、写证据；要求主工作区干净
uv run team evolve prompt analyzer               # 生成分析 agent 提示词
```
把 `prompt analyzer` 的输出**原样**作为 prompt，派一个 `general-purpose` subagent（**只读**，不能复用开发/评审）。它读取：
- `shared/artifacts/evolve/<run>/evidence/insights.md`（选中建议全文）；
- 仓库代码，逐条核实目标是否还存在、问题是否已修复；

并写出 `shared/artifacts/evolve/<run>/analysis.json`：`merges`（同义合并）、`dismissed`（建议驳回及理由）、`groups`（组名、标题、计划、建议 id、**互不相交的文件集**、依赖）。

收到分析结果后：
1. 抽查 1~3 条证据/驳回理由，确认不是拍脑袋；
2. 检查分组是否真的互不影响（同文件必须同组）；
3. 运行校验：
```bash
uv run team evolve group --from shared/artifacts/evolve/<run>/analysis.json
```
校验失败（漏项/重复分配/文件相交/依赖成环/非法路径）会直接报错且不落盘：把报错原样发回**同一个分析 agent** 修，直到通过。
4. 把分组表（组名、建议、文件、依赖、驳回项）给用户看一眼；用户已授权自动合并的话不必等确认。

`dismissed` 只是分析 agent 的提议；主流程确认后，由第 6 步 `finish` 统一写回建议池，**不要在开发阶段提前标 dismissed/applied**。

## 3. 并行开发（每组一个 worktree + 一个 subagent）
对每组：
```bash
uv run team evolve worktree <group>              # .worktrees/<run>/<group>，分支 evolve/<run>/<group>
```
然后**在同一条消息里**为全部组各派一个 `general-purpose` subagent（多于 5 组分批，每批同一条消息发出），prompt 用：
```bash
uv run team evolve prompt developer <group>      # 已填好 worktree、边界、建议全文、报告路径
```
原样使用，不要删减准则段落；需要补充上下文就追加在末尾。

subagent 交付后：
```bash
uv run team evolve record developer <group>      # 记录 HEAD 与改动条数
uv run team evolve check <group>                 # 门禁：组边界、commit 前缀、交付契约、JSON/plan/diff/pytest/ruff
```
门禁不过：把 `groups/<group>/gate-r<N>.json` 里的失败项原样转给**同组开发 agent**（SendMessage）修，修完再 `record developer` + `check`。同一组门禁连续 3 次不过就放弃：
```bash
uv run team evolve log abandoned --group <group> --field reason=门禁连续 3 次不过
```

## 4. 独立评审（每组一个新的只读 subagent）
门禁通过的组：
```bash
uv run team evolve prompt reviewer <group>       # 轮次自动递增
```
各派一个**新的** `general-purpose` subagent（不能复用开发 agent；只读，不 commit）。评审写出 `groups/<group>/review-r<N>.json` 与 `.md`，然后：
```bash
uv run team evolve record review <group>         # 校验 verdict；PASS 但仍有未修复 blocking 会被拒绝
```

修复轮（每组最多 2 轮）：
- `FAIL`：`uv run team evolve log fix_round --group <group>`，把评审的 blocking/major 和非阻塞里低风险高价值的项一起发给**原开发 agent**（SendMessage），要求追加 `fix(<group>): …` commit、更新 `report.json`。修完 `record developer` → `check` → 用 `uv run team evolve prompt reviewer <group>` 生成增量复审提示词发给**原评审员**（只看上轮 HEAD 之后的增量）→ `record review`。
- `PASS` 但有非阻塞建议：低风险高价值的（措辞冲突、明显误差、口径矛盾）也按上面走一轮修复+复审；其余在评审文件里标 `deferred`，进报告遗留项。
- 第 2 轮修复后仍 FAIL：`fix_round` 会被拒绝，按准则放弃：
```bash
uv run team evolve log abandoned --group <group> --field reason=…
```
该组不合并，分支保留备查，组内建议保持 open。

## 5. 合并（自动，本地主分支，不 push）
```bash
uv run team evolve merge                         # 只合 approved 且门禁通过的组，按 depends_on 拓扑 --no-ff 合入，再跑主分支全量门禁并清理 worktree
```
- 返回 `conflict` 非空：停下，把冲突组和文件告诉用户，不自动解冲突、不 force。
- 主分支门禁失败：停下报告失败项（`main-gate.json`），不要回滚别人的提交；和用户确认后再处理。
- 永远不 push、不改远端。

## 6. 收尾与回写
```bash
uv run team evolve finish                        # result.json + report.md，并把落地/驳回结果写回建议池
git add coordinator/insights/backlog.json
git commit -m "chore(evolve): <run> 建议池状态" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```
- 合并组的建议标 `applied`（note 写 run/组/merge commit）；分析阶段驳回的标 `dismissed`（note 写理由）；合并分析里的重复建议指向主建议；未落地组的建议保持 `open`。
- 只提交 `backlog.json`；run 明细与报告在 `shared/artifacts/evolve/<run>/`，不入库。
- 先 `uv run team evolve finish --dry-run` 看将回写哪些状态，确认无误再正式执行。

## 7. 汇报
用中文说清：选中了哪些建议、分析阶段合并/驳回了什么、分成几组；每组结论（合并/放弃）与核心改动；门禁与评审轮次、遗留项；主分支门禁结果与合并 commit；建议池回写结果（applied/dismissed/open 各多少）；报告路径。

## 准则（编排者必须守住）
- **分析/开发/评审三角独立**：分析 agent 只读，评审 agent 必须是新的只读 subagent，不能复用开发 agent；修复后必须复审。
- **边界由代码校验**：组只能改 `analysis.json` 里声明的文件；门禁 `scope` 失败一律修，不要手动扩大边界绕过。
- **互不影响是硬约束**：各组文件集必须两两不相交；需要改同一文件（含测试）的建议必须同组。
- **证据先行**：开发与评审都要回指建议原文/证据；驳回必须写理由，不能静默丢弃。
- **不越权**：不 push、不删历史 Job/产物、不改 `coordinator/state/`；主仓有他人改动时先问。
- **状态延迟回写**：只在 `finish` 改建议池，避免「已标 applied 但分支没合」。
