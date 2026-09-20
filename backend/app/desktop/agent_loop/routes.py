r"""本文件对外提供 agent_loop_router，作为 Loop、Mission、授权、类型化等待响应、用户介入、Kernel decision、控制与事件 HTTP 边界。

输入为已通过 Desktop 会话认证的结构化 Mission 或兼容旧字段及其它严格 schema；输出为 Loop snapshot、
持久用户意图、Kernel result 或 cursor event。具体工作流为路由解析 Mission 后从 app.state 取得专用
service/Kernel，不在 HTTP 边界写领域状态或运行模型。
示例：`app.include_router(agent_loop_router)`。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, LoopCreateRequest, LoopGrantMutationRequest, LoopInterventionRequest, LoopWaitResponseRequest, PatrolDecisionIntent
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter, LoopMissionContract
from backend.app.desktop.agent_loop.kernel import KernelRejected


agent_loop_router = APIRouter(prefix="/desktop/api/agent-loops", tags=["agent-loops"])


class LoopControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str


class LoopOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission: LoopMissionContract | None = None
    goal: str | None = Field(default=None, min_length=1)
    task_contract: str | None = Field(default=None, min_length=1)
    acceptance_criteria: list[dict[str, Any]] | None = Field(default=None, min_length=1)

    def resolved_mission(self) -> LoopMissionContract:
        if self.mission is not None:
            if any(value is not None for value in (self.goal, self.task_contract, self.acceptance_criteria)):
                raise ValueError("mission 与旧覆盖字段不能同时提交")
            return self.mission
        if self.goal is None or self.task_contract is None or self.acceptance_criteria is None:
            raise ValueError("必须提交 mission 或完整旧覆盖字段")
        return LegacyMissionAdapter.convert(
            goal=self.goal,
            task_contract=self.task_contract,
            acceptance_criteria=self.acceptance_criteria,
        )


class LoopMissionRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["activate"]
    mission: LoopMissionContract


class CompletionEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_request_id: str
    verification: CompletionVerificationContract


@agent_loop_router.post("")
async def start_loop(body: LoopCreateRequest, request: Request) -> dict:
    return await request.app.state.agent_loop_service.start(body)


@agent_loop_router.get("/by-context/{context_id}")
async def get_active_loop_for_context(context_id: str, request: Request) -> dict | None:
    return await request.app.state.agent_loop_service.active_for_context(context_id)


@agent_loop_router.get("/activation-eligibility/by-context/{context_id}")
async def get_loop_activation_eligibility(context_id: str, request: Request) -> dict:
    return await request.app.state.agent_loop_service.activation_eligibility(context_id)


@agent_loop_router.get("/{loop_id}")
async def get_loop(loop_id: str, request: Request) -> dict:
    return await request.app.state.agent_loop_service.get(loop_id)


@agent_loop_router.get("/{loop_id}/wait-request")
async def get_loop_wait_request(loop_id: str, request: Request) -> dict | None:
    return await request.app.state.agent_loop_service.active_wait_request(loop_id)


@agent_loop_router.post("/{loop_id}/wait-requests/{request_id}/responses")
async def respond_to_loop_wait_request(
    loop_id: str,
    request_id: str,
    body: LoopWaitResponseRequest,
    request: Request,
) -> dict:
    return await request.app.state.agent_loop_service.resolve_wait_request(
        loop_id,
        request_id,
        body,
        actor_id="user",
    )


@agent_loop_router.post("/{loop_id}/control")
async def control_loop(loop_id: str, body: LoopControlRequest, request: Request) -> dict:
    return await request.app.state.agent_loop_service.control(loop_id, body.command)


@agent_loop_router.post("/{loop_id}/grant")
async def mutate_loop_grant(loop_id: str, body: LoopGrantMutationRequest, request: Request) -> dict:
    await request.app.state.agent_loop_authority.mutate(loop_id, body)
    return await request.app.state.agent_loop_service.get(loop_id)


@agent_loop_router.post("/{loop_id}/override")
async def override_loop(loop_id: str, body: LoopOverrideRequest, request: Request) -> dict:
    try:
        mission = body.resolved_mission()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await request.app.state.agent_loop_service.override(loop_id, mission=mission)


@agent_loop_router.post("/{loop_id}/missions")
async def activate_loop_mission(
    loop_id: str,
    body: LoopMissionRevisionRequest,
    request: Request,
) -> dict:
    return await request.app.state.agent_loop_service.override(loop_id, mission=body.mission)


@agent_loop_router.post("/{loop_id}/interventions")
async def submit_loop_intervention(
    loop_id: str,
    body: LoopInterventionRequest,
    request: Request,
) -> dict:
    return await request.app.state.agent_loop_interventions.submit(loop_id, body)


@agent_loop_router.post("/{loop_id}/decisions")
async def commit_decision(loop_id: str, body: PatrolDecisionIntent, request: Request) -> dict:
    if body.loop_id != loop_id:
        raise HTTPException(422, "path loop_id 与 decision loop_id 不一致")
    try:
        result = await request.app.state.agent_loop_kernel.commit(body)
    except KernelRejected as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"decision_id": result.decision_id, "status": result.status, "action_ids": result.action_ids, "directive_ids": result.directive_ids, "reason": result.reason}


@agent_loop_router.get("/{loop_id}/events")
async def loop_events(loop_id: str, request: Request, after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    return await request.app.state.agent_loop_service.events(loop_id, after, limit)


@agent_loop_router.get("/{loop_id}/events/stream")
async def stream_loop_events(loop_id: str, request: Request, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    async def stream():
        cursor = after
        idle_ticks = 0
        while not await request.is_disconnected():
            events = await request.app.state.agent_loop_service.events(loop_id, cursor, 200)
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = max(cursor, int(event["cursor"]))
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event['event_id']}\nevent: {event['type']}\ndata: {payload}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks >= 30:
                    idle_ticks = 0
                    yield f": cursor={cursor}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@agent_loop_router.post("/{loop_id}/completion-evidence")
async def record_completion_evidence(loop_id: str, body: CompletionEvidenceRequest, request: Request) -> dict:
    if body.verification.loop_id != loop_id:
        raise HTTPException(422, "path loop_id 与 verification loop_id 不一致")
    result = await request.app.state.agent_loop_completion.record(body.verification, body.worker_request_id)
    return result.model_dump(mode="json")
