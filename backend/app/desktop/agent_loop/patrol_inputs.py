"""本文件对外提供 PatrolInputRequest、PatrolInputRepository 和 input_identity。

输入为工作区用户提交的四类原文、稳定提交身份和可选补充请求目标；输出为耐久 accepted 记录或冲突及分页历史。
具体工作流为在调用方 Loop 事务内验证全局提交身份、保存原文类型和有界可见线索、记录既有 intervention 生命周期，
回执保留引入的不可变观察及输入前快照身份；不恢复图、不取消 Run、不更新 Mission。示例：await repository.accept(session, loop, request)。
同时提供 UserInputUse、MissionInputChange、ApplyUserInputsAction 和 workspace_input_scope；已保存文件模式仅用于首次授权，后续信息不扩大权限。
file_refs 可选绑定 Fact revision、Artifact result_key、slot/path 和内容 hash；接受时校验同工作区及证据权限，空引用保持旧请求 hash。
attachments 保存用户选择的材料身份和来源；服务端另外冻结精确内容 manifest，空附件不改变旧请求 hash，纯附件保留空正文。
history 追加已应用的逐输入 processing_result；intent_ids 仅在原工作区范围内刷新已加载旧记录，不改变受理语义。
"""

import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from focus.history import content_hash
from backend.app.desktop.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse


class VisibleFactReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fact_id: str = Field(min_length=1, max_length=32)
    revision: int = Field(ge=1)


class VisibleRunPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(min_length=1, max_length=32)
    context_id: str = Field(min_length=1, max_length=32)
    message_id: str | None = Field(default=None, max_length=160)
    sequence: int | None = Field(default=None, ge=0)
    role: Literal["human", "assistant", "tool", "system"] = "assistant"
    text: str = Field(max_length=4096)
    truncated: bool = False
    gap: bool = False


class VisibleContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    loop_id: str = Field(min_length=1, max_length=32)
    live_sequence: int | None = Field(default=None, ge=0)
    fact_refs: tuple[VisibleFactReference, ...] = Field(default=(), max_length=256)
    run_previews: tuple[VisibleRunPreview, ...] = Field(default=(), max_length=32)


class FileReference(VisibleFactReference):
    result_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    slot_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkspaceAttachmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    material_id: str = Field(min_length=1, max_length=32)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: Literal["image", "file", "text"]
    note: str = Field(default="", max_length=4000)


class PatrolInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    submission_id: str = Field(min_length=1, max_length=160)
    input_type: Literal["information", "outcome", "boundary", "completion_check"] = "information"
    content: str = Field(default="", max_length=12000)
    access_mode: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    request_id: str | None = None
    request_revision: int | None = Field(default=None, gt=0)
    visible_context: VisibleContext | None = None
    file_refs: tuple[FileReference, ...] = Field(default=(), max_length=32)
    attachments: tuple[WorkspaceAttachmentReference, ...] = Field(default=(), max_length=32)
    budget_preset: Literal["normal", "long"] | None = None

    @model_validator(mode="after")
    def _validate_input(self):
        if not self.content.strip() and not self.attachments:
            raise ValueError("输入不能为空白")
        if len({item.material_id for item in self.attachments}) != len(self.attachments):
            raise ValueError("附件材料不能重复")
        if (self.request_id is None) != (self.request_revision is None):
            raise ValueError("回答必须同时指定请求身份和版本")
        return self


class UserInputUse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: str = Field(min_length=1)
    disposition: Literal["discussion", "decision", "reference", "deferred"]
    explanation: str = Field(min_length=1, max_length=1000)


class MissionInputChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: str
    section: Literal["outcome", "boundary", "completion_check"]
    quote: str = Field(min_length=1, max_length=12000)
    operation: Literal["supplement", "revise", "remove"] = "supplement"
    supersedes: tuple[str, ...] = ()
    boundary_group: Literal["in_scope", "required_invariants", "prohibited_actions"] = (
        "required_invariants"
    )
    evidence_kinds: tuple[
        Literal["test", "tool", "artifact", "workspace", "fact", "user"], ...
    ] = ()


class ApplyUserInputsAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["apply_user_inputs"]
    inputs: tuple[UserInputUse, ...] = Field(min_length=1)
    changes: tuple[MissionInputChange, ...] = ()
    resolved_request_ids: tuple[str, ...] = ()


def input_identity(submission_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"focus:patrol-input:{submission_id}").hex


def request_document(request: PatrolInputRequest) -> dict:
    exclude = {"visible_context"} if request.visible_context is None else set()
    if not request.file_refs:
        exclude.add("file_refs")
    if not request.attachments:
        exclude.add("attachments")
    if request.budget_preset is None:
        exclude.add("budget_preset")
    return request.model_dump(mode="json", exclude=exclude)


class PatrolInputRepository:
    def __init__(self):
        self._lifecycle = InterventionLifecycleRepository()

    async def replay(self, session, workspace_id, request):
        row = await session.get(LoopUserIntent, input_identity(request.submission_id))
        if row is None:
            return None
        expected = content_hash([workspace_id, request_document(request)])
        if (
            row.intent_kind != "workspace_input"
            or row.request_payload.get("request_hash") != expected
        ):
            raise HTTPException(409, "提交身份已用于不同工作区、类型、原文或回答目标")
        return self.view(row)

    async def accept(self, session, loop, request):
        replay = await self.replay(session, loop.workspace_id, request)
        if replay is not None:
            return replay
        from backend.app.desktop.agent_loop.workspace_attachments import WorkspaceAttachments

        attachments = await WorkspaceAttachments().freeze(session, loop, request)
        if request.file_refs:
            from backend.app.desktop.domain_evidence.file_reads import FileVersionReader

            for reference in request.file_refs:
                await FileVersionReader().validate_reference(session, loop.workspace_id, reference)
        if request.request_id:
            target = await session.get(LoopWaitRequest, request.request_id, with_for_update=True)
            if (
                target is None
                or target.loop_id != loop.loop_id
                or target.kind != "clarification"
                or not target.scope.get("information_only")
                or target.status != "open"
                or target.revision != request.request_revision
            ):
                raise HTTPException(409, "补充信息请求已失效或不属于当前工作区")
            answered = await session.scalar(
                select(LoopWaitResponse).where(LoopWaitResponse.request_id == target.request_id)
            )
            if answered is not None:
                raise HTTPException(409, "该请求已有耐久回答，等待 Patrol 重新判断")
            session.add(
                LoopWaitResponse(
                    response_id=uuid.uuid4().hex,
                    request_id=target.request_id,
                    actor_id="user",
                    answer={"text": request.content, "submission_id": request.submission_id},
                    request_revision=target.revision,
                    idempotency_key=f"patrol-input:{request.submission_id}",
                )
            )
        row = LoopUserIntent(
            intent_id=input_identity(request.submission_id),
            loop_id=loop.loop_id,
            scope="portfolio",
            content=request.content,
            intent_kind="workspace_input",
            origin_kind="user",
            status="pending",
            correlation_id=input_identity(request.submission_id),
            goal_revision=loop.goal_revision,
            authority_revision=loop.authority_revision,
            request_payload={
                "request": request_document(request),
                "request_hash": content_hash([loop.workspace_id, request_document(request)]),
                **({"attachments": attachments} if attachments else {}),
            },
        )
        session.add(row)
        await self._lifecycle.register(session, row)
        await self._lifecycle.transition(session, row.intent_id, "accepted")
        return self.view(row)

    async def history(self, session, workspace_id, *, before=None, limit=50, intent_ids=None):
        query = (
            select(LoopUserIntent)
            .join(AgentLoop, AgentLoop.loop_id == LoopUserIntent.loop_id)
            .where(
                AgentLoop.workspace_id == workspace_id,
                LoopUserIntent.intent_kind == "workspace_input",
            )
        )
        if intent_ids is not None:
            query = query.where(LoopUserIntent.intent_id.in_(intent_ids))
        if before:
            cursor = await session.scalar(query.where(LoopUserIntent.intent_id == before))
            if cursor is None:
                raise HTTPException(422, "历史游标不属于当前工作区")
            from sqlalchemy import tuple_

            query = query.where(
                tuple_(LoopUserIntent.created_at, LoopUserIntent.intent_id)
                < tuple_(cursor.created_at, cursor.intent_id)
            )
        rows = tuple(
            await session.scalars(
                query.order_by(
                    LoopUserIntent.created_at.desc(), LoopUserIntent.intent_id.desc()
                ).limit(limit + 1)
            )
        )
        page = rows[:limit]
        from backend.app.desktop.agent_loop.input_feedback import PatrolInputFeedbackReader

        feedback = await PatrolInputFeedbackReader().read(session, workspace_id, page)
        return {
            "items": [{**self.view(row), "processing_result": feedback.get(row.intent_id)}
                      for row in page],
            "has_more": len(rows) > limit,
            "next_before": page[-1].intent_id if len(rows) > limit and page else None,
        }

    @staticmethod
    def view(row):
        request = row.request_payload.get("request") or {}
        return {
            "intent_id": row.intent_id,
            "loop_id": row.loop_id,
            "submission_id": request.get("submission_id"),
            "input_type": request.get("input_type", "information"),
            "content": row.content,
            "request_id": request.get("request_id"),
            "request_revision": request.get("request_revision"),
            "file_refs": request.get("file_refs", []),
            **({"attachments": row.request_payload["attachments"]} if row.request_payload.get("attachments") else {}),
            "round_id": row.observed_round_id,
            "observation_id": row.introduced_observation_id,
            "introduced_generation": row.introduced_generation,
            "snapshot_hash": row.snapshot_hash,
            "accepted_sequence": row.request_payload.get("accepted_sequence"),
            "status": "accepted",
            "processing_state": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


def workspace_input_scope(loop):
    loops = select(AgentLoop.loop_id).where(
        AgentLoop.workspace_id == loop.workspace_id,
        AgentLoop.interaction_mode == "workspace_patrol",
        (AgentLoop.loop_id == loop.loop_id)
        | AgentLoop.status.in_(("completed", "stopped", "failed")),
    )
    return (LoopUserIntent.loop_id == loop.loop_id) | (
        (LoopUserIntent.intent_kind == "workspace_input") & LoopUserIntent.loop_id.in_(loops)
    )
