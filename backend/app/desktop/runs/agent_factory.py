"""AgentFactory 的业务实现与显式依赖端口。"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Protocol

from focus.agents.image_attachment import build_image_attachment_middleware
from focus.agents.lead import make_lead_agent
from focus.agents.material_inputs import RunMaterialInputs
from focus.agents.must_view import (
    build_must_view_completion_middleware,
    report_must_view_images,
)
from focus.tools import get_available_tools
from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool
from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool
from focus.tools.builtins.web_tools import web_fetch, web_search
from focus.tools.builtins.workspace_tools import select_workspace_tools
from focus.tools.catalog import execution_pool_tools
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from backend.app.desktop.agents.inbox import AgentInbox, DurableInboxMiddleware
from backend.app.desktop.capabilities.skills import (
    build_task_skill_catalog,
    describe_task_skill_catalog,
)
from backend.app.desktop.runs.context_assembly import (
    desktop_contexts,
    skill_catalog_records,
)
from backend.app.desktop.runs.execution_attempts import (
    ModelAttemptJournal,
    ModelAttemptMiddleware,
)
from backend.app.desktop.runs.tool_errors import build_tool_error_middleware


class AgentFactoryPorts(Protocol):
    def _build_patrol_reader_tools(self, task_id: str) -> list[BaseTool]: ...

    def _build_swarm_reader_tools(self, task_id: str) -> list[BaseTool]: ...

    def _build_swarm_tools(self) -> list[BaseTool]: ...

    def _compression_context_window(self, model_name: str | None) -> int | None: ...

    agent_collab: Any
    app_config: Any
    checkpointer: Any
    session_factory: Any


class AgentFactory:
    def __init__(self, ports: AgentFactoryPorts) -> None:
        self._ports = ports

    def _build_agent_factory(
        self,
        task_id: str,
        agent_id: str,
        workspace_path: str,
        equipment: dict[str, Any],
        base_prompt: str,
        material_context: str,
        agent_role: str,
        preparation_observer=None,
    ) -> Callable[[], Awaitable[CompiledStateGraph]]:
        ports = self._ports
        permissions = equipment.get("permissions") or ["read"]
        model_name = equipment.get("model_name")
        governed_loop = "_loop_authority_revision" in equipment
        collab_tools = ports.agent_collab.build_collab_tools(agent_role)
        include_mailbox = agent_role in ("main", "teammate", "worker")

        async def factory() -> CompiledStateGraph:
            tools = select_workspace_tools(permissions)
            if os.name == "nt" and hasattr(ports, "browsers"):
                from backend.app.desktop.browser.tools import build_browser_tools

                tools = [*tools, *build_browser_tools()]
            if agent_role != "patrol":
                tools = [*tools, web_search, web_fetch]
            if agent_role == "main":
                pooled = await get_available_tools(workspace=workspace_path)
                pool_tools = execution_pool_tools(pooled, include_builtin=False)
                if os.name == "nt" and hasattr(ports, "browsers"):
                    pool_tools = [
                        item
                        for item in pool_tools
                        if not item.name.startswith("browser_")
                    ]
                tools = [
                    *tools,
                    *ports._build_patrol_reader_tools(task_id),
                    *ports._build_swarm_reader_tools(task_id),
                    *(
                        []
                        if governed_loop
                        else [build_spawn_agent_tool(), *ports._build_swarm_tools()]
                    ),
                    *pool_tools,
                ]
            tools = [*tools, *collab_tools]
            catalog = build_task_skill_catalog(workspace_path)
            if agent_role == "main":
                tools = [
                    *tools,
                    build_describe_skill_tool(describe_task_skill_catalog(catalog)),
                ]
                task_skill_names = frozenset(catalog)
                middlewares = None
            else:
                task_skill_names = None
                middlewares = []
            additional_middlewares = [build_tool_error_middleware()]
            if os.name == "nt" and hasattr(ports, "browsers"):
                from backend.app.desktop.browser.middleware import (
                    BrowserControlMiddleware,
                )

                additional_middlewares.append(BrowserControlMiddleware())
            user_input_middleware = None
            if agent_role == "main" and (not governed_loop):
                from backend.app.desktop.conversations.input_delivery import (
                    ConversationInputDelivery,
                )

                user_input_middleware = ConversationInputDelivery(
                    ports.session_factory, ports.checkpointer
                )
            if agent_role == "main":
                image_inputs = RunMaterialInputs.from_equipment(equipment).images
                additional_middlewares.append(build_image_attachment_middleware())
                if image_inputs.required_ids:
                    additional_middlewares.append(
                        build_must_view_completion_middleware()
                    )
                    tools = [*tools, report_must_view_images]
            if agent_role == "main" and ports.app_config.compression.enabled:
                from focus.agents.compression.gate import build_compression_gate

                additional_middlewares.append(
                    build_compression_gate(
                        context_window=ports._compression_context_window(model_name),
                        threshold_ratio=ports.app_config.compression.threshold_ratio,
                    )
                )
            return await make_lead_agent(
                model_name=model_name,
                **{"reasoning_effort": equipment["reasoning_effort"]}
                if equipment.get("reasoning_effort") is not None
                else {},
                tools=tools,
                system_prompt=base_prompt,
                middlewares=middlewares,
                additional_middlewares=additional_middlewares,
                user_input_middleware=user_input_middleware,
                app_config=ports.app_config,
                middleware_skill_names=task_skill_names,
                frozen_contexts=desktop_contexts(equipment, material_context),
                world_skill_catalog=skill_catalog_records(catalog),
                inbox_middleware=DurableInboxMiddleware(
                    AgentInbox(ports.session_factory, ports.checkpointer)
                )
                if include_mailbox
                else None,
                attempt_middleware=ModelAttemptMiddleware(
                    ModelAttemptJournal(ports.session_factory, ports.checkpointer)
                ),
                preparation_observer=preparation_observer,
            )

        return factory
