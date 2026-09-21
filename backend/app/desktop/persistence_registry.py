r"""本文件对外提供 Desktop 各领域 SQLAlchemy 模型的统一注册入口。

输入为 Python 模块导入；输出为已完整注册到 `Base.metadata` 的桌面、Context Evolution、Curation、
Run、Workspace、Agent Loop、Context expansion、Wait request、Projection recovery、Mission contract、Patrol Session、版本化 Fact、规范事件日志与自主压缩权威表。具体工作流为仅在组合根和迁移工具导入各领域模型模块，
领域服务不通过本文件访问实体，本入口也不导入协调器或触发运行时行为。
示例：`import backend.app.desktop.persistence_registry`。
"""

import backend.app.desktop.context_evolution.models  # noqa: F401
import backend.app.desktop.context_curation.models  # noqa: F401
import backend.app.desktop.run_orchestration.models  # noqa: F401
import backend.app.desktop.workspace_coordination.models  # noqa: F401
import backend.app.desktop.agent_loop.models  # noqa: F401
import backend.app.desktop.agent_loop.context_expansion.models  # noqa: F401
import backend.app.desktop.agent_loop.activation_models  # noqa: F401
import backend.app.desktop.agent_loop.wait_models  # noqa: F401
import backend.app.desktop.agent_loop.projection_models  # noqa: F401
import backend.app.desktop.agent_loop.mission_models  # noqa: F401
import backend.app.desktop.agent_loop.journal_models  # noqa: F401
import backend.app.desktop.agent_loop.fact_models  # noqa: F401
import backend.app.desktop.agent_loop.patrol_session_models  # noqa: F401
import backend.app.desktop.agent_loop.compression_authority.models  # noqa: F401
import backend.app.desktop.models  # noqa: F401
