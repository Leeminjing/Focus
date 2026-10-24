"""PatrolService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import json
from typing import Any, Protocol

from fastapi import HTTPException
from focus.security.effects import NO_LOCAL_EFFECT, declare_all_effects
from focus.security.policy import AccessMode
from langchain_core.tools import BaseTool, tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.api.schemas import ContextCurationPolicy, DraftUpdate
from backend.app.desktop.capabilities.skills import (
    build_task_skill_catalog,
    prompt_with_skills,
    resolve_task_skills,
)
from backend.app.desktop.contexts.curator import (
    CurationEngineError,
    CurationSourceProjector,
    build_curation_input,
    estimate_curation_tokens,
)
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    PatrolAgent,
    PatrolDraft,
)
from backend.app.desktop.persistence.values import normalize_json_storage_value
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.estimates import estimate_tokens
from backend.app.gateway.routers.thread_runs import RunCreateRequest


class PatrolServicePorts(Protocol):
    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    def _normalize_equipment(self, equipment: dict[str, Any]) -> dict[str, Any]: ...

    @staticmethod
    def _normalize_skill_names(value: Any) -> list[str]: ...

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]: ...

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None: ...

    app_config: Any
    checkpointer: Any
    context_patrol: Any
    contexts: Any

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]: ...

    session_factory: Any
    session_patrol: Any


class PatrolService:
    def __init__(self, ports: PatrolServicePorts) -> None:
        self._ports = ports

    async def open_draft(self, task_id: str) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task, _ = await ports._get_task_entities(session, task_id)
            existing = await session.scalar(
                select(PatrolDraft)
                .where(PatrolDraft.task_id == task_id, PatrolDraft.status == "editing")
                .order_by(PatrolDraft.updated_at.desc())
            )
            if existing:
                payload = self._draft_payload(existing)
                if existing.mode == "context_curator":
                    payload["root_context_id"] = await ports.contexts.resolve_chat_root(
                        task_id
                    )
                return payload
            from focus.history import serialize_history_message

            from backend.app.desktop.contexts.evolution.reader import (
                ContextRevisionReader,
            )
            from backend.app.desktop.contexts.evolution.repository import (
                ContextRevisionRepository,
            )
            from backend.app.desktop.patrol.session_patrol.contracts import (
                AuthoringDocument,
            )
            from backend.app.desktop.patrol.session_patrol.source_projection import (
                freeze_authoring_sources,
            )

            repository = ContextRevisionRepository()
            revision = await repository.current(session, task_id)
            checkpoint_id = revision.ref.checkpoint_id if revision else None
            if revision:
                view = await ContextRevisionReader(repository, ports.checkpointer).read(
                    session, revision.ref, "execution"
                )
                messages = (
                    [
                        item.model_dump(mode="json")
                        for item in revision.history_payload.execution_items
                    ]
                    if revision.history_payload is not None
                    else list(view.messages)
                )
            else:
                config = {
                    "configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}
                }
                checkpoint = await ports.checkpointer.aget_tuple(config)
                checkpoint_id = (
                    checkpoint.config["configurable"].get("checkpoint_id")
                    if checkpoint
                    else None
                )
                values = (
                    checkpoint.checkpoint.get("channel_values", {})
                    if checkpoint
                    else {}
                )
                messages = (
                    values.get("execution_items")
                    if values.get("execution_items") is not None
                    else [
                        serialize_history_message(message)
                        for message in values.get("messages", [])
                    ]
                )
            source = (
                revision.ref.model_dump(mode="json")
                if revision
                else {
                    "context_id": task_id,
                    "execution_thread_id": task.thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": checkpoint_id,
                    "legacy": True,
                }
            )
            entries, frozen_sources = freeze_authoring_sources(
                messages, {**source, "title": task.title}
            )
            document = AuthoringDocument(entries=entries)
            messages = []
            default_model = ports.app_config.resolve_default_model_name()
            equipment = {
                "model_name": default_model,
                "skills": [],
                "permissions": ["read"],
                "access_mode": str(AccessMode.WORKSPACE),
            }
            draft = PatrolDraft(
                draft_id=new_id(),
                task_id=task_id,
                history_messages=messages,
                authoring_document=document.model_dump(mode="json"),
                frozen_sources=frozen_sources,
                source_checkpoint_id=checkpoint_id,
                equipment=equipment,
                token_estimate=estimate_tokens("", messages, ""),
            )
            session.add(draft)
            await session.commit()
            return self._draft_payload(draft)

    async def update_draft(self, draft_id: str, body: DraftUpdate) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            from backend.app.desktop.patrol.session_patrol.repository import (
                DraftRepository,
            )

            policy_only = body.mode == "context_curator" and (
                not body.model_fields_set
                & {"system_prompt", "history_messages", "final_human_message"}
            )
            draft = await DraftRepository().save(
                session,
                draft_id,
                body.authoring_document,
                expected_revision=body.draft_revision,
                policy_only=policy_only,
            )
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可编辑草稿不存在")
            task_row, workspace = await ports._get_task_entities(session, draft.task_id)
            equipment = normalize_json_storage_value(
                ports._normalize_equipment(body.equipment)
            )
            if body.mode == "context_curator":
                forbidden = set(equipment.get("permissions") or []) & {
                    "write",
                    "host_command",
                }
                if forbidden:
                    raise HTTPException(
                        422, "Context 策展模式禁止 write 与 host_command 权限"
                    )
                if equipment.get("skills"):
                    raise HTTPException(422, "Context 策展模式不装配技能或通用工具")
                policy = ContextCurationPolicy.model_validate(body.curation_policy)
                root_context_id = await self._prepare_context_curator_draft(
                    draft, equipment.get("model_name"), policy
                )
                await session.commit()
                payload = self._draft_payload(draft)
                payload["root_context_id"] = root_context_id
                return payload
            if body.authoring_document is not None:
                draft.equipment = equipment
                draft.mode = "standard"
                await session.commit()
                return self._draft_payload(draft)
            safe_system_prompt = normalize_json_storage_value(body.system_prompt)
            safe_history_messages = normalize_json_storage_value(body.history_messages)
            safe_final_human_message = normalize_json_storage_value(
                body.final_human_message
            )
            snapshots, _ = resolve_task_skills(
                build_task_skill_catalog(workspace.path), equipment["skills"]
            )
            estimate = estimate_tokens(
                prompt_with_skills(safe_system_prompt, snapshots),
                safe_history_messages,
                safe_final_human_message,
            )
            draft.system_prompt = safe_system_prompt
            draft.history_messages = safe_history_messages
            draft.final_human_message = safe_final_human_message
            draft.equipment = equipment
            draft.mode = "standard"
            draft.curation_policy = {}
            draft.token_estimate = estimate
            await session.commit()
            return self._draft_payload(draft)

    async def quick_deploy_context_curator(
        self, task_id: str, deployment_id: str
    ) -> PreparedRun:
        ports = self._ports
        async with ports.session_factory() as session:
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.deployment_id == deployment_id)
            )
            if existing:
                return PreparedRun(
                    body=RunCreateRequest(input={"messages": []}),
                    thread_id="",
                    agent_factory=None,
                    payload=ports._run_payload(existing),
                )
            await ports._get_task_entities(session, task_id)
            draft = PatrolDraft(
                draft_id=new_id(),
                task_id=task_id,
                status="editing",
                mode="context_curator",
                system_prompt="",
                history_messages=[],
                final_human_message="",
                equipment={},
                curation_policy={},
                token_estimate=0,
            )
            await self._prepare_context_curator_draft(
                draft, self._default_curation_model_name(), ContextCurationPolicy()
            )
            session.add(draft)
            await session.commit()
            draft_id = draft.draft_id
        return await self.deploy(draft_id, deployment_id)

    async def _prepare_context_curator_draft(
        self, draft: PatrolDraft, model_name: str | None, policy: ContextCurationPolicy
    ) -> str:
        ports = self._ports
        root_context_id = await ports.contexts.resolve_chat_root(draft.task_id)
        checkpoint_id = await ports.contexts.current_checkpoint_id(root_context_id)
        if checkpoint_id is None:
            raise HTTPException(409, "根 Context 尚无稳定 checkpoint")
        snapshot = await ports.contexts.semantic_snapshot(
            root_context_id, checkpoint_id
        )
        try:
            ports.context_patrol.engine.validate_model(model_name)
        except CurationEngineError as exc:
            raise HTTPException(
                422, {"code": "curation_model_incompatible", "message": str(exc)}
            ) from exc
        source = CurationSourceProjector().project(checkpoint_id, snapshot["messages"])
        curation_input = build_curation_input(source, 0, policy, [])
        estimate = estimate_curation_tokens(curation_input)
        model = ports.app_config.get_model(
            model_name or ports.app_config.resolve_default_model_name()
        )
        ports._validate_model_window(
            model_name, estimate + model.curation_max_output_tokens
        )
        draft.mode = "context_curator"
        draft.curation_policy = normalize_json_storage_value(
            policy.model_dump(mode="json")
        )
        draft.system_prompt = ""
        draft.history_messages = []
        draft.final_human_message = ""
        draft.equipment = {
            "model_name": model_name,
            "skills": [],
            "permissions": ["read"],
            "access_mode": str(AccessMode.WORKSPACE),
        }
        draft.source_checkpoint_id = checkpoint_id
        draft.token_estimate = estimate
        return root_context_id

    def _default_curation_model_name(self) -> str:
        ports = self._ports
        configured = [
            model for model in ports.app_config.models if model.curation_default
        ]
        if not configured:
            raise HTTPException(
                422,
                {
                    "code": "curation_default_model_missing",
                    "message": "尚未配置 Context 策展默认模型",
                },
            )
        model = configured[0]
        try:
            ports.context_patrol.engine.validate_model(model.name)
        except CurationEngineError as exc:
            raise HTTPException(
                422,
                {"code": "curation_default_model_incompatible", "message": str(exc)},
            ) from exc
        return model.name

    async def deploy(
        self, draft_id: str, deployment_id: str, preview_token: str | None = None
    ) -> PreparedRun:
        ports = self._ports
        async with ports.session_factory() as session:
            draft_mode = await session.scalar(
                select(PatrolDraft.mode).where(PatrolDraft.draft_id == draft_id)
            )
        if draft_mode == "context_curator":
            run = await ports.context_patrol.deploy(draft_id, deployment_id)
            return PreparedRun(
                body=RunCreateRequest(input={"messages": []}),
                thread_id="",
                agent_factory=None,
                payload=ports._run_payload(run),
            )
        return await ports.session_patrol.deploy(draft_id, deployment_id, preview_token)

    async def retry_agent(self, agent_id: str) -> PreparedRun:
        ports = self._ports
        return await ports.session_patrol.restart(agent_id)

    async def continue_agent(
        self,
        agent_id: str,
        message: str,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> PreparedRun:
        ports = self._ports
        return await ports.session_patrol.continue_from(
            agent_id, message, source_run_id=run_id, checkpoint_id=checkpoint_id
        )

    async def list_agents(self, task_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            agents = (
                await session.scalars(
                    select(PatrolAgent)
                    .where(PatrolAgent.task_id == task_id)
                    .order_by(PatrolAgent.created_at)
                )
            ).all()
            result = []
            for agent in agents:
                latest = await session.scalar(
                    select(DesktopRun)
                    .where(DesktopRun.agent_id == agent.agent_id)
                    .order_by(DesktopRun.created_at.desc())
                )
                result.append(
                    {
                        "agent_id": agent.agent_id,
                        "checkpoint_ns": agent.checkpoint_ns,
                        "mode": agent.mode,
                        "curation_policy": agent.curation_policy,
                        "permissions": agent.equipment.get("permissions", ["read"]),
                        "created_at": agent.created_at.isoformat()
                        if agent.created_at
                        else None,
                        "latest_run": ports._run_payload(latest) if latest else None,
                    }
                )
        for item in result:
            if item["mode"] == "context_curator":
                detail = await ports.context_patrol.detail(item["agent_id"], limit=1)
                item.update(
                    {
                        "control_state": detail["control_state"],
                        "health_state": detail["health_state"],
                        "root_context": detail["root_context"],
                        "managed_context": detail["managed_context"],
                        "observed_checkpoint_id": detail["observed_checkpoint_id"],
                        "desired_checkpoint_id": detail["desired_checkpoint_id"],
                        "prepared_checkpoint_id": detail["prepared_checkpoint_id"],
                        "published_checkpoint_id": detail["published_checkpoint_id"],
                        "latest_revision": detail["latest_revision"],
                        "last_error": detail["last_error"],
                    }
                )
        return result

    async def agent_history(self, agent_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
            task = await session.get(DesktopThread, agent.task_id)
            mode = agent.mode
        if mode == "context_curator":
            return await ports.context_patrol.history(agent_id)
        async with ports.session_factory() as session:
            latest = await session.scalar(
                select(DesktopRun)
                .where(DesktopRun.agent_id == agent_id)
                .order_by(DesktopRun.created_at.desc())
            )
            execution_thread = (
                latest.execution_thread_id
                if latest and latest.execution_thread_id
                else task.thread_id
            )
            execution_namespace = (
                latest.checkpoint_ns if latest else agent.checkpoint_ns
            )
        return await ports.get_checkpoint_messages(
            execution_thread, execution_namespace
        )

    def _build_patrol_reader_tools(self, task_id: str) -> list[BaseTool]:

        @tool
        async def list_patrol_agents() -> str:
            """列出当前任务中用户投放的小兵及其最新已提交运行状态。"""
            return json.dumps(await self.list_agents(task_id), ensure_ascii=False)

        @tool
        async def read_patrol_agent_history(agent_id: str) -> str:
            """读取当前任务指定小兵已提交的完整消息历史，不包含草稿和实时 token。"""
            agents = await self.list_agents(task_id)
            if agent_id not in {item["agent_id"] for item in agents}:
                raise ValueError("该小兵不属于当前任务")
            return json.dumps(await self.agent_history(agent_id), ensure_ascii=False)

        return declare_all_effects(
            [list_patrol_agents, read_patrol_agent_history], NO_LOCAL_EFFECT
        )

    def _draft_payload(self, draft: PatrolDraft) -> dict[str, Any]:
        ports = self._ports
        from backend.app.desktop.patrol.session_patrol.repository import DraftRepository

        default_policy = ContextCurationPolicy().model_dump(mode="json")
        equipment = {
            **draft.equipment,
            "skills": ports._normalize_skill_names(draft.equipment.get("skills")),
        }
        equipment.pop("skill_snapshots", None)
        return {
            "draft_id": draft.draft_id,
            "task_id": draft.task_id,
            "status": draft.status,
            "authoring_document": DraftRepository.document(draft).model_dump(
                mode="json"
            ),
            "draft_revision": draft.draft_revision,
            "history_schema_version": 3,
            "mode": draft.mode,
            "curation_policy": draft.curation_policy
            or (default_policy if draft.mode == "context_curator" else {}),
            "default_curation_policy": default_policy,
            "default_curation_model_name": next(
                (
                    model.name
                    for model in ports.app_config.models
                    if model.curation_default
                ),
                None,
            ),
            "system_prompt": draft.system_prompt,
            "history_messages": draft.history_messages,
            "final_human_message": draft.final_human_message,
            "equipment": equipment,
            "source_checkpoint_id": draft.source_checkpoint_id,
            "token_estimate": draft.token_estimate,
        }
