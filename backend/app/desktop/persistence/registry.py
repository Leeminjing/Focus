r"""本文件对外提供 Desktop 各领域 SQLAlchemy 模型的统一注册入口。

输入为 Python 模块导入；输出为已完整注册到 `Base.metadata` 的桌面、Context Evolution、Curation、
Run、Workspace、Agent Loop、Context expansion、Wait request、Projection recovery、Mission contract、Patrol Session、版本化 Fact、任务记忆、独立领域来源、规范事件日志与自主压缩权威表。具体工作流为仅在组合根和迁移工具导入各领域模型模块，
领域服务不通过本文件访问实体，本入口也不导入协调器或触发运行时行为。
示例：`import backend.app.desktop.persistence.registry`。
"""

import backend.app.desktop.patrol.agent_loop.governance.activation_models
import backend.app.desktop.patrol.agent_loop.compression_authority.models
import backend.app.desktop.patrol.agent_loop.context_expansion.models
import backend.app.desktop.patrol.agent_loop.facts.fact_models
import backend.app.desktop.patrol.agent_loop.journal.journal_models
import backend.app.desktop.patrol.agent_loop.missions.mission_models
import backend.app.desktop.patrol.agent_loop.models
import backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs
import backend.app.desktop.patrol.agent_loop.sessions.patrol_session_models
import backend.app.desktop.patrol.agent_loop.projections.projection_models
import backend.app.desktop.patrol.agent_loop.task_progress.models
import backend.app.desktop.patrol.agent_loop.journal.timeline_models
import backend.app.desktop.patrol.agent_loop.waits.wait_models
import backend.app.desktop.contexts.curation.models
import backend.app.desktop.conversations.inputs
import backend.app.desktop.contexts.evolution.models
import backend.app.desktop.runs.evidence.models
import backend.app.desktop.persistence.models
import backend.app.desktop.runs.orchestration.models
import backend.app.desktop.workspaces.coordination.models  # noqa: F401
