"""普通会话的耐久补充与下一轮队列。

输入为绑定 Run 的用户补充或冻结的 MainRunCreate；输出为独立交付回执。
补充只在精确 checkpoint 包含原消息后确认，队列按任务串行、按提交顺序启动；
启动与回执跨事务时用稳定 Run 幂等键恢复，撤回和派发争用同一消息行锁。
"""

from datetime import datetime
import uuid

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from typing import Literal

from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.api.schemas import MainRunCreate, StrictRequest
from backend.app.desktop.persistence.safety import PersistencePayloadNormalizer
from backend.app.desktop.runs.materials import RunMaterialRequest
from backend.app.desktop.conversations.message_reference import validate_message_reference
from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
from focus.history import content_hash
from focus.persistence.base import Base


class ConversationInput(Base):
    __tablename__ = "conversation_inputs"
    __table_args__ = (
        CheckConstraint("mode IN ('steer', 'queue')", name="ck_conversation_input_mode"),
        CheckConstraint("status IN ('accepted','queued','delivered','started','not_delivered','blocked','withdrawn')",
                        name="ck_conversation_input_status"),
        Index("ix_conversation_input_task_order", "task_id", "created_at", "input_id"),
    )
    input_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    target_run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="SET NULL"))
    run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="SET NULL"))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    checkpoint_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class ConversationInputCreate(StrictRequest):
    mode: Literal["steer", "queue"]
    target_run_id: str | None = Field(default=None, max_length=32)
    run: MainRunCreate

    @model_validator(mode="after")
    def validate_steer(self):
        if self.mode == "queue" and self.target_run_id is not None:
            raise ValueError("排队消息不绑定当前 Run")
        if self.mode == "steer":
            if not self.target_run_id:
                raise ValueError("补充当前轮需要指定 Run")
            if not isinstance(self.run.message, str) or not self.run.message.strip():
                raise ValueError("补充要求不能为空")
            if self.run.material_inputs or self.run.attached_material_ids or self.run.must_view_material_ids or self.run.skills:
                raise ValueError("当前轮补充仅支持文字；新增材料或技能请排队到下一轮")
        return self


def input_payload(row):
    return {"input_id": row.input_id, "task_id": row.task_id, "mode": row.mode, "status": row.status,
            "target_run_id": row.target_run_id, "run_id": row.run_id, "run": row.payload,
            "error": row.error, "checkpoint_id": row.checkpoint_id}


class ConversationInputs:
    def __init__(self, desktop):
        self.desktop = desktop
        self.sessions = desktop.session_factory

    @staticmethod
    async def ordinary_session(session, task_id):
        from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopContextMembership
        loop_id = await session.scalar(select(AgentLoop.loop_id).join(LoopContextMembership,
            LoopContextMembership.loop_id == AgentLoop.loop_id).where(
            LoopContextMembership.context_id == task_id, LoopContextMembership.status.in_(("active", "paused")),
            AgentLoop.status.in_(("running", "paused", "waiting_user"))).limit(1))
        return loop_id is None

    async def submit(self, task_id, body, request_id):
        await validate_message_reference(self.desktop, task_id, body.run.message_reference)
        identity = uuid.uuid5(uuid.NAMESPACE_URL, f"focus:conversation-input:{task_id}:{request_id}").hex
        frozen = body.model_dump(mode="json")
        request_hash = content_hash(frozen)
        async with self.sessions.begin() as session:
            # 与 Run admission 同锁，防止在旧 Run 结算后误投下一轮。
            await session.execute(select(func.pg_advisory_xact_lock(RunAdmissionService._lock_key("task", task_id))))
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            existing = await session.get(ConversationInput, identity)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise HTTPException(409, "同一请求身份不能更改消息内容")
                return input_payload(existing)
            if not await self.ordinary_session(session, task_id):
                raise HTTPException(409, "此入口仅用于普通会话；当前任务由 Loop 管理")
            if body.mode == "steer":
                run = await session.get(DesktopRun, body.target_run_id, with_for_update=True)
                if run is None or run.task_id != task_id or run.kind != "main" or run.loop_id or run.status not in {"pending", "running"}:
                    raise HTTPException(409, "目标当前轮已结束或不属于普通会话，请选择排队下一轮")
            row = ConversationInput(input_id=identity, task_id=task_id, mode=body.mode,
                target_run_id=body.target_run_id, status="accepted" if body.mode == "steer" else "queued",
                request_hash=request_hash,
                payload=PersistencePayloadNormalizer.normalize(frozen["run"], "conversation-input").value)
            session.add(row)
            await session.flush()
            return input_payload(row)

    async def list(self, task_id):
        async with self.sessions() as session:
            rows = (await session.scalars(select(ConversationInput).where(ConversationInput.task_id == task_id,
                ConversationInput.status != "withdrawn").order_by(ConversationInput.created_at, ConversationInput.input_id))).all()
            return [input_payload(row) for row in rows]

    async def withdraw(self, task_id, input_id):
        async with self.sessions.begin() as session:
            row = await session.get(ConversationInput, input_id, with_for_update=True)
            if row is None or row.task_id != task_id:
                raise HTTPException(404, "消息不存在")
            if row.mode != "queue" or row.status not in {"queued", "blocked", "withdrawn"}:
                raise HTTPException(409, "消息已开始执行，不能撤回")
            admitted = await session.scalar(select(DesktopRun.run_id).where(
                DesktopRun.idempotency_key == f"conversation-queue:{row.input_id}"))
            if admitted is not None:
                # Run 已提交而消息回执尚未提交的重启窗口也不能伪报撤回成功。
                raise HTTPException(409, {"message": "消息已开始执行，不能撤回", "run_id": admitted})
            row.status = "withdrawn"
            return input_payload(row)

    async def drain(self):
        async with self.sessions() as session:
            task_ids = (await session.scalars(select(ConversationInput.task_id).where(
                ConversationInput.status.in_(("queued", "accepted"))).distinct())).all()
        for task_id in task_ids:
            async with self.sessions.begin() as session:
                # 独立锁避免两个派发器越过队首；不占用 Main admission 的 task 锁。
                locked = await session.scalar(select(func.pg_try_advisory_xact_lock(
                    RunAdmissionService._lock_key("conversation-queue", task_id))))
                if not locked:
                    continue
                rows = (await session.scalars(select(ConversationInput).where(ConversationInput.task_id == task_id,
                    ConversationInput.status.in_(("queued", "accepted"))).order_by(
                    ConversationInput.created_at, ConversationInput.input_id).with_for_update())).all()
                for row in rows:
                    if row.mode == "steer":
                        target = await session.get(DesktopRun, row.target_run_id) if row.target_run_id else None
                        if target is None or target.settled_at is not None:
                            row.status, row.error = "not_delivered", "当前轮已结束，补充未进入模型上下文"
                queued = next((row for row in rows if row.mode == "queue"), None)
                if queued is None:
                    continue
                # 若上次已准入但进程在回写回执前退出，恢复原 Run。
                key = f"conversation-queue:{queued.input_id}"
                existing = await self.desktop.run_by_idempotency(key)
                if existing:
                    queued.status, queued.run_id = "started", existing["run_id"]
                    continue
                blocked = await session.scalar(select(ConversationInput.input_id).where(
                    ConversationInput.task_id == task_id, ConversationInput.status == "blocked").limit(1))
                if blocked:
                    continue
                if not await self.ordinary_session(session, task_id):
                    queued.status, queued.error = "blocked", "当前任务已转由 Loop 管理，请撤回排队消息"
                    continue
                active = await session.scalar(select(DesktopRun.run_id).where(DesktopRun.task_id == task_id,
                    DesktopRun.kind == "main", or_(DesktopRun.status.in_(("pending", "running")),
                                                   DesktopRun.settled_at.is_(None))).limit(1))
                if active:
                    continue
                latest = await session.scalar(select(DesktopRun).where(DesktopRun.task_id == task_id,
                    DesktopRun.kind == "main").order_by(DesktopRun.created_at.desc()).limit(1))
                if latest is not None and latest.status != "success":
                    queued.status, queued.error = "blocked", "上一轮未成功结束，请撤回编辑或删除后再继续"
                    continue
                body = MainRunCreate.model_validate(queued.payload)
                try:
                    prepared = await self.desktop.start_main_run(task_id=task_id, message=body.message,
                        message_reference=body.message_reference.model_dump() if body.message_reference else None,
                        model_name=body.model_name, permissions=body.permissions, skills=body.skills,
                        reasoning_effort=body.reasoning_effort,
                        spatial_focus=body.spatial_focus, memory_ids=body.memory_ids, access_mode=body.access_mode,
                        material_inputs=[RunMaterialRequest(material_id=item.material_id, note=item.note)
                                         for item in body.material_inputs] if body.material_inputs is not None else None,
                        attached_material_ids=body.attached_material_ids, must_view_material_ids=body.must_view_material_ids,
                        run_identity={"message_id": queued.input_id, "origin": "direct_user", "idempotency_key": key},
                        admit_only=True)
                except HTTPException as error:
                    if isinstance(error.detail, dict) and error.detail.get("code") == "main_run_active":
                        continue
                    queued.status, queued.error = "blocked", str(error.detail)
                else:
                    queued.status, queued.run_id = "started", prepared.payload["run_id"]
