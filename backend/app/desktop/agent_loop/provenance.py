r"""本文件对外提供 DelegatedDirectiveFactory 与 MessageHistoryProjector。

输入为已授权 directive 事实或持久消息/provenance；输出为模型可见的纯 HumanMessage 与 UI 可见的
来源投影。具体工作流为来源只写外部表，to_model_message 仅设置 id/content，绝不加入 Patrol、grant、
delegated metadata 或 system prompt。示例：`message = factory.to_model_message(directive)`。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from backend.app.desktop.agent_loop.models import LoopDirective, MessageProvenance


class DelegatedDirectiveFactory:
    @staticmethod
    def create(
        *,
        loop_id: str,
        round_id: str,
        decision_id: str,
        action_id: str,
        context_id: str,
        context_revision_id: str,
        content: str,
        actor_id: str,
        grant_id: str,
        grant_revision: int,
        goal_revision: int,
        idempotency_key: str,
    ) -> tuple[LoopDirective, MessageProvenance]:
        message_id = uuid.uuid4().hex
        directive = LoopDirective(
            directive_id=uuid.uuid4().hex,
            loop_id=loop_id,
            round_id=round_id,
            decision_id=decision_id,
            action_id=action_id,
            target_context_id=context_id,
            target_context_revision_id=context_revision_id,
            message_id=message_id,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            actor_id=actor_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            goal_revision=goal_revision,
            idempotency_key=idempotency_key,
        )
        provenance = MessageProvenance(
            provenance_id=uuid.uuid4().hex,
            context_revision_id=context_revision_id,
            message_id=message_id,
            source_kind="delegated_patrol",
            actor_id=actor_id,
            directive_id=directive.directive_id,
            audit={"loop_id": loop_id, "round_id": round_id, "grant_revision": grant_revision},
        )
        return directive, provenance

    @staticmethod
    def to_model_message(directive: LoopDirective) -> HumanMessage:
        return HumanMessage(id=directive.message_id, content=directive.content)


class MessageHistoryProjector:
    @staticmethod
    def project(message: dict[str, Any], provenance: MessageProvenance | None) -> dict[str, Any]:
        return {
            **message,
            "provenance": None if provenance is None else {
                "source_kind": provenance.source_kind,
                "actor_id": provenance.actor_id,
                "directive_id": provenance.directive_id,
                "audit": provenance.audit,
            },
        }
