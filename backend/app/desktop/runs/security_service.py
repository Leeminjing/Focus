"""RunSecurityService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from focus.security.context import (
    AuthorizationIdentity,
    ExecutionProfile,
    RoutingIdentity,
    security_context_of,
)
from focus.security.launch import assemble_run_context
from focus.security.policy import AccessMode, workspace_roots

from backend.app.desktop.capabilities.equipment import _resolve_access_mode
from backend.app.desktop.capabilities.secrets import configured_secret_values
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.runs.execution_attempts import ToolExecutionLedger


class RunSecurityServicePorts(Protocol):
    app_config: Any
    browsers: Any
    session_factory: Any


class RunSecurityService:
    def __init__(self, ports: RunSecurityServicePorts) -> None:
        self._ports = ports

    def _attach_session_mode_resolver(
        self, context: dict[str, Any], run: DesktopRun, equipment: dict[str, Any]
    ) -> None:
        security = security_context_of(context)
        resolver_name = (
            "round_mode_resolver" if run.loop_id else "session_mode_resolver"
        )
        resolver = (
            self._round_mode_resolver(run.run_id)
            if run.loop_id
            else self._session_mode_resolver(
                run.task_id, _resolve_access_mode(equipment.get("access_mode"))
            )
        )
        context["security_context"] = replace(
            security,
            extras={
                **security.extras,
                resolver_name: resolver,
                **(
                    {"round_authorization": equipment.get("round_authorization")}
                    if run.loop_id
                    else {}
                ),
            },
        )

    def _round_mode_resolver(self, run_id: str):
        ports = self._ports

        async def current() -> AccessMode:
            from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import (
                RunOwnershipPolicy,
            )

            async with ports.session_factory() as session:
                run = await session.get(DesktopRun, run_id)
                if run is None:
                    raise RuntimeError("所属 Round 的 Run 已不存在")
                await RunOwnershipPolicy().assert_live(session, run)
                return _resolve_access_mode((run.equipment or {}).get("access_mode"))

        return current

    def _session_mode_resolver(self, task_id: str, initial: AccessMode):
        ports = self._ports

        async def current() -> AccessMode:
            async with ports.session_factory() as session:
                task = await session.get(DesktopThread, task_id)
                if task is None:
                    raise RuntimeError(f"会话已不存在: {task_id}")
                saved = (task.ui_state or {}).get("access_mode")
                return _resolve_access_mode(saved) if saved else initial

        return current

    def _governed_context(
        self,
        *,
        thread_id: str,
        run: DesktopRun,
        workspace_id: str,
        workspace_path: str,
        permissions: list[str],
        access_mode: str | None,
        checkpoint_ns: str,
        agent_role: str,
        model_name: str | None,
        allow_global_config: bool,
        extras: dict[str, Any],
        swarm_depth: int = 0,
    ) -> dict[str, Any]:
        ports = self._ports
        profile = ExecutionProfile(
            authorization=AuthorizationIdentity(
                workspace=Path(workspace_path),
                roots=workspace_roots(
                    Path(workspace_path), allow_global_config=allow_global_config
                ),
                permissions=tuple(permissions),
                access_mode=AccessMode(str(access_mode))
                if access_mode
                else AccessMode.READ_ONLY,
                agent_role=agent_role,
            ),
            routing=RoutingIdentity(
                thread_id=thread_id,
                workspace_id=workspace_id,
                agent_id=run.agent_id,
                task_id=run.task_id,
                checkpoint_ns=checkpoint_ns,
                run_id=run.run_id,
            ),
            model_name=model_name,
        )
        runtime_extras = {
            **extras,
            "context_revision_ref": {
                "revision_id": run.context_revision_id,
                "checkpoint_id": run.context_checkpoint_id,
                "context_id": run.task_id,
            },
        }
        runtime_extras["origin_message_id"] = run.origin_message_id
        if hasattr(ports, "session_factory"):
            from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import (
                RunOwnershipPolicy,
            )

            runtime_extras["tool_execution_ledger"] = ToolExecutionLedger(
                ports.session_factory,
                authority_validator=RunOwnershipPolicy().assert_live,
                secret_provider=lambda: configured_secret_values(ports.app_config),
            )
        context = assemble_run_context(
            profile, runtime_extras, dispatch_hints={"swarm_depth": swarm_depth}
        )
        if os.name == "nt" and hasattr(ports, "browsers"):
            resource = ports.browsers.resource(run.task_id, run.run_id, profile.routing)
            security = security_context_of(context)
            context["security_context"] = replace(
                security,
                extras={
                    **security.extras,
                    "browser_run": resource,
                    "run_resource": resource,
                },
            )
        return context
