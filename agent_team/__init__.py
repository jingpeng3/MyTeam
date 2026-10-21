"""agent_team —— 基于 herdr 的多 agent 团队编排框架。

设计要点：
- 每个 agent 是 agents/<name>/ 目录，由 agent.yaml 声明式定义。
- 协调者是当前交互会话；本包提供其调度所需的支撑工具。
- herdr 管 pane 生命周期，文件信号(inbox/outbox)管任务语义。
"""

__version__ = "0.1.0"
