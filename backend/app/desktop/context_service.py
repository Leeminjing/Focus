"""
本文件对外提供 ContextService，负责桌面 Context 的快照、派生、lineage、执行投影审批
与独立 checkpoint 初始化。

输入为桌面 session factory、LangGraph checkpointer、AppConfig 以及 Context 请求模型；
输出为可直接返回给桌面 API 的 Context 数据。具体工作流为校验同工作区来源和已提交
checkpoint，保存用户 authored messages，调用 context_projection 编译执行投影，在无需
降级或用户批准后以新 thread_id 调用 aupdate_state 创建独立 checkpoint。示例：
`context = await service.derive(body)`。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_projection import ContextProjection, compile_context_messages
from backend.app.desktop.models import (
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    DesktopContextDefinition,
    DesktopContextSource,
    DesktopRun,
    DesktopThread,
)
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import deserialize_messages, serialize_message, validate_messages


_RUNNABLE_STATUSES = frozenset({"valid", "repaired", "approved"})


def _new_id() -> str:
    return uuid.uuid4().hex


class ContextService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        app_config: AppConfig,
    ) -> None:
        self.session_factory = session_factory
        self.checkpointer = checkpointer
        self.app_config = app_config

    async def snapshot(self, context_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(DesktopContextDefinition, context_id)
        if definition and definition.initial_checkpoint_id is None and checkpoint_id is None:
            return {
                "context_id": context_id,
                "checkpoint_id": None,
                "messages": deepcopy(definition.authored_messages),
            }
        config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        if checkpoint_id:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            if checkpoint_id:
                raise HTTPException(404, "Context checkpoint 不存在")
            messages = deepcopy(definition.authored_messages) if definition else []
            return {"context_id": context_id, "checkpoint_id": None, "messages": messages}
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id and actual_id != checkpoint_id:
            raise HTTPException(404, "Context checkpoint 不属于该 Context")
        values = checkpoint.checkpoint.get("channel_values", {})
        runtime_messages = [serialize_message(message) for message in values.get("messages", [])]
        messages = (
            self._display_messages(definition, runtime_messages)
            if definition and (not checkpoint_id or checkpoint_id == definition.initial_checkpoint_id)
            else runtime_messages
        )
        return {"context_id": context_id, "checkpoint_id": actual_id, "messages": messages}

    async def derive(self, body: ContextDeriveCreate) -> dict[str, Any]:
        refs = [(source.context_id, source.checkpoint_id) for source in body.sources]
        if len(refs) != len(set(refs)):
            raise HTTPException(422, "Context 来源不能重复")
        async with self.session_factory() as session:
            parents = [await session.get(DesktopThread, context_id) for context_id, _ in refs]
            if any(parent is None for parent in parents):
                raise HTTPException(404, "来源 Context 不存在")
            workspace_ids = {parent.workspace_id for parent in parents if parent is not None}
            if len(workspace_ids) != 1:
                raise HTTPException(422, "只能合并同一工作区的 Context")
        for parent, (_, checkpoint_id) in zip(parents, refs):
            await self._require_checkpoint(parent.thread_id, checkpoint_id)

        projection = compile_context_messages(body.messages)
        context_id = _new_id()
        thread_id = _new_id()
        async with self.session_factory() as session:
            task = DesktopThread(
                task_id=context_id,
                workspace_id=next(iter(workspace_ids)),
                thread_id=thread_id,
                title=body.title,
                ui_state={},
            )
            session.add(task)
            await session.flush()
            definition = DesktopContextDefinition(
                context_id=context_id,
                authored_messages=projection.authored_messages,
                execution_messages=projection.execution_messages,
                repair_manifest=projection.repair_manifest,
                issues=projection.issues,
                definition_hash=projection.definition_hash,
                projection_hash=projection.projection_hash,
                projection_status=projection.status,
                initial_message_ids=[],
            )
            sources = [
                DesktopContextSource(
                    source_id=_new_id(),
                    context_id=context_id,
                    parent_context_id=source.context_id,
                    source_checkpoint_id=source.checkpoint_id,
                    position=index,
                )
                for index, source in enumerate(body.sources)
            ]
            session.add_all([definition, *sources])
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(context_id)

    async def update_definition(
        self, context_id: str, body: ContextDefinitionUpdate
    ) -> dict[str, Any]:
        projection = compile_context_messages(body.messages)
        async with self.session_factory() as session:
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == context_id)
                .with_for_update()
            )
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            has_main_run = await session.scalar(
                select(DesktopRun.run_id)
                .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                .limit(1)
            )
            if has_main_run:
                raise HTTPException(409, "Context 已开始运行；请从当前快照继续派生")
            self._apply_projection(definition, projection)
            if projection.status != "approval_required":
                definition.projection_status = "initializing"
            definition.decision = None
            definition.decided_definition_hash = None
            definition.decided_projection_hash = None
            definition.decided_at = None
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(context_id)

    async def decide(
        self, context_id: str, body: ContextProjectionDecision
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            definition = await session.get(DesktopContextDefinition, context_id)
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            if (
                body.definition_hash != definition.definition_hash
                or body.projection_hash != definition.projection_hash
            ):
                raise HTTPException(409, "Context 定义或拟议投影已经变化，请重新确认")
            if definition.initial_checkpoint_id:
                raise HTTPException(409, "Context 执行 checkpoint 已初始化")
            definition.decision = body.decision
            definition.decided_definition_hash = body.definition_hash
            definition.decided_projection_hash = body.projection_hash
            definition.decided_at = datetime.now(timezone.utc)
            if body.decision == "reject":
                definition.projection_status = "rejected"
            else:
                try:
                    validate_messages(definition.execution_messages)
                except ValueError as exc:
                    raise HTTPException(422, f"拟议投影仍不合法，不能批准：{exc}") from exc
                definition.projection_status = "initializing"
            await session.commit()
        if body.decision == "accept":
            await self._initialize(context_id, "approved")
        return await self.get(context_id)

    async def get(self, context_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(DesktopContextDefinition, context_id)
            sources = (
                await session.scalars(
                    select(DesktopContextSource)
                    .where(DesktopContextSource.context_id == context_id)
                    .order_by(DesktopContextSource.position)
                )
            ).all()
            has_main_run = bool(
                await session.scalar(
                    select(DesktopRun.run_id)
                    .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                    .limit(1)
                )
            )
            return self._payload(task, definition, sources, editable=bool(definition) and not has_main_run)

    async def lineage(self, context_id: str) -> dict[str, Any]:
        payload = await self.get(context_id)
        async with self.session_factory() as session:
            depth = await self._depth(session, context_id, {})
        return {"context_id": context_id, "depth": depth, "sources": payload["sources"]}

    async def tree(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            tasks = (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.workspace_id == workspace_id)
                    .order_by(DesktopThread.created_at)
                )
            ).all()
            source_rows = (
                await session.scalars(
                    select(DesktopContextSource)
                    .where(DesktopContextSource.context_id.in_([task.task_id for task in tasks]))
                    .order_by(DesktopContextSource.context_id, DesktopContextSource.position)
                )
            ).all() if tasks else []
            definitions = {
                item.context_id: item
                for item in (
                    await session.scalars(
                        select(DesktopContextDefinition).where(
                            DesktopContextDefinition.context_id.in_([task.task_id for task in tasks])
                        )
                    )
                ).all()
            } if tasks else {}
            run_stats = {
                task_id: (int(input_tokens or 0), int(hit_tokens or 0))
                for task_id, input_tokens, hit_tokens in (
                    await session.execute(
                        select(
                            DesktopRun.task_id,
                            func.sum(DesktopRun.prompt_input_tokens),
                            func.sum(DesktopRun.prompt_cache_hit_tokens),
                        )
                        .where(
                            DesktopRun.task_id.in_([task.task_id for task in tasks]),
                            DesktopRun.kind == "main",
                        )
                        .group_by(DesktopRun.task_id)
                    )
                ).all()
            } if tasks else {}
            by_child: dict[str, list[DesktopContextSource]] = {}
            for source in source_rows:
                by_child.setdefault(source.context_id, []).append(source)
            memo: dict[str, int] = {}

            def depth(context_id: str) -> int:
                if context_id in memo:
                    return memo[context_id]
                sources = by_child.get(context_id, [])
                memo[context_id] = 0 if not sources else depth(sources[0].parent_context_id) + 1
                return memo[context_id]

            result = []
            for task in tasks:
                input_tokens, hit_tokens = run_stats.get(task.task_id, (0, 0))
                result.append({
                    "context_id": task.task_id,
                    "task_id": task.task_id,
                    "thread_id": task.thread_id,
                    "title": task.title,
                    "depth": depth(task.task_id),
                    "projection_status": (
                        definitions[task.task_id].projection_status if task.task_id in definitions else "root"
                    ),
                    "editable": task.task_id in definitions and task.task_id not in run_stats,
                    "cache_input_tokens": input_tokens,
                    "cache_hit_tokens": hit_tokens,
                    "cache_hit_rate": hit_tokens / input_tokens if input_tokens else None,
                    "parents": [self._source_payload(source) for source in by_child.get(task.task_id, [])],
                })
            return result

    async def ensure_runnable(self, session: AsyncSession, context_id: str) -> None:
        definition = await session.scalar(
            select(DesktopContextDefinition)
            .where(DesktopContextDefinition.context_id == context_id)
            .with_for_update()
        )
        if definition and definition.projection_status not in _RUNNABLE_STATUSES:
            raise HTTPException(
                409,
                {
                    "code": "context_projection_blocked",
                    "message": "Context 执行投影尚未得到安全确认",
                    "context": self._definition_payload(definition),
                },
            )

    async def _initialize(self, context_id: str, ready_status: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            definition = await session.get(DesktopContextDefinition, context_id)
            if not task or not definition:
                raise HTTPException(404, "Context 不存在")
            config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
            existing = await self.checkpointer.aget_tuple(config)
            execution_messages = deepcopy(definition.execution_messages)
            replace_existing = False
            if existing is not None:
                values = existing.checkpoint.get("channel_values", {})
                stored = [serialize_message(message) for message in values.get("messages", [])]
                if self._comparable_messages(stored) != self._comparable_messages(execution_messages):
                    replace_existing = True
            definition.projection_status = "initializing"
            await session.commit()
        try:
            if existing is None or replace_existing:
                graph = await self._make_state_graph()
                messages = deserialize_messages(execution_messages)
                for raw, message in zip(execution_messages, messages):
                    if raw.get("curation_synthetic"):
                        message.additional_kwargs["curation_synthetic"] = True
                update = (
                    [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
                    if replace_existing
                    else messages
                )
                updated_config = await graph.aupdate_state(
                    config,
                    {"messages": update},
                    as_node="model" if replace_existing else None,
                )
                state = await graph.aget_state(updated_config)
                checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
                initial_ids = [message.id for message in state.values.get("messages", []) if message.id]
            else:
                checkpoint_id = existing.config.get("configurable", {}).get("checkpoint_id")
                values = existing.checkpoint.get("channel_values", {})
                initial_ids = [message.id for message in values.get("messages", []) if message.id]
        except Exception as exc:
            async with self.session_factory() as session:
                definition = await session.get(DesktopContextDefinition, context_id)
                if definition:
                    definition.projection_status = "initialization_failed"
                    definition.issues = [*definition.issues, {"index": None, "reason": str(exc)}]
                    await session.commit()
            return
        async with self.session_factory() as session:
            definition = await session.get(DesktopContextDefinition, context_id)
            if not definition:
                return
            definition.initial_checkpoint_id = checkpoint_id
            definition.initial_message_ids = initial_ids
            definition.projection_status = ready_status
            await session.commit()

    async def _make_state_graph(self):
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=self.app_config
        )
        graph.checkpointer = self.checkpointer
        return graph

    async def _require_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
        )
        actual = checkpoint.config.get("configurable", {}).get("checkpoint_id") if checkpoint else None
        if actual != checkpoint_id:
            raise HTTPException(404, "来源 checkpoint 不存在或不属于指定 Context")

    async def _depth(self, session: AsyncSession, context_id: str, memo: dict[str, int]) -> int:
        if context_id in memo:
            return memo[context_id]
        source = await session.scalar(
            select(DesktopContextSource)
            .where(DesktopContextSource.context_id == context_id)
            .order_by(DesktopContextSource.position)
        )
        memo[context_id] = 0 if not source else await self._depth(session, source.parent_context_id, memo) + 1
        return memo[context_id]

    @staticmethod
    def _apply_projection(definition: DesktopContextDefinition, projection: ContextProjection) -> None:
        definition.authored_messages = projection.authored_messages
        definition.execution_messages = projection.execution_messages
        definition.repair_manifest = projection.repair_manifest
        definition.issues = projection.issues
        definition.definition_hash = projection.definition_hash
        definition.projection_hash = projection.projection_hash
        definition.projection_status = projection.status
        definition.initial_message_ids = []
        definition.initial_checkpoint_id = None

    @staticmethod
    def _display_messages(
        definition: DesktopContextDefinition | None,
        runtime_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if definition is None:
            return runtime_messages
        initial_ids = set(definition.initial_message_ids or [])
        suffix = [message for message in runtime_messages if message.get("id") not in initial_ids]
        return [*deepcopy(definition.authored_messages), *suffix]

    @staticmethod
    def _comparable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = deepcopy(messages)
        for message in result:
            message.pop("id", None)
            message.pop("locked", None)
            message.pop("curation_synthetic", None)
        return result

    @classmethod
    def _payload(
        cls,
        task: DesktopThread,
        definition: DesktopContextDefinition | None,
        sources: list[DesktopContextSource],
        *,
        editable: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "context_id": task.task_id,
            "task_id": task.task_id,
            "workspace_id": task.workspace_id,
            "thread_id": task.thread_id,
            "title": task.title,
            "projection_status": "root",
            "sources": [cls._source_payload(source) for source in sources],
            "editable": editable,
        }
        if definition:
            payload.update(cls._definition_payload(definition))
        return payload

    @staticmethod
    def _definition_payload(definition: DesktopContextDefinition) -> dict[str, Any]:
        return {
            "projection_status": definition.projection_status,
            "authored_messages": deepcopy(definition.authored_messages),
            "execution_messages": deepcopy(definition.execution_messages),
            "repair_manifest": deepcopy(definition.repair_manifest),
            "issues": deepcopy(definition.issues),
            "definition_hash": definition.definition_hash,
            "projection_hash": definition.projection_hash,
            "initial_checkpoint_id": definition.initial_checkpoint_id,
            "decision": definition.decision,
        }

    @staticmethod
    def _source_payload(source: DesktopContextSource) -> dict[str, Any]:
        return {
            "context_id": source.parent_context_id,
            "checkpoint_id": source.source_checkpoint_id,
            "position": source.position,
        }
