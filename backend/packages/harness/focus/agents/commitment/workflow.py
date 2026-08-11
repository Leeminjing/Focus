"""
本文件对外提供人工恢复流程、delegate_with_review 工具和 Supervisor 子图构建函数。

输入:
    CommitmentState — 当前阶段、人工等待标记、阶段产物和 thread 上下文。
    ReviewedDelegator — 执行 Worker-Evaluator 审核的内部委派器。
    approve / revise resume — 人工批准、反馈或 replacement 数据。

输出:
    BaseTool — Supervisor 唯一可调用的 delegate_with_review 工具。
    CompiledStateGraph — 严格按 stage 1 到 9 推进的 Supervisor 子图。
    Command — 阶段推进、人工等待、知识合同落盘及消息更新。

具体工作流:
    (1) Supervisor 根据最后完成阶段构造下一份 TaskEnvelope。
    (2) delegate_with_review 校验 stage 后调用 ReviewedDelegator。
    (3) 固定人工节点、条件冲突或失败结果通过 interrupt 等待人工处理。
    (4) 人工修订重新经过 Evaluator，未批准前不推进业务阶段。
    (5) 阶段六写入知识；阶段七人工确认后写入合同；阶段九生成最终合同消息。
    (6) Supervisor messages 只保留阶段委派和最终工具结果。

示例:
    supervisor = _build_supervisor(ReviewedDelegator(model, context7_tools))
"""

import asyncio
import json
from typing import Any

from langchain.messages import AIMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from focus.agents.commitment.artifacts import (
    _build_final_message,
    _write_contract,
    _write_knowledge,
)
from focus.agents.commitment.delegation import ReviewedDelegator
from focus.agents.commitment.schemas import (
    CommitmentState,
    TaskEnvelope,
    WorkerOutput,
)
from focus.agents.commitment.stage_rules import (
    _HUMAN_REVIEW_STAGES,
    _contains_unresolved_versions,
    _has_open_conflicts,
    _json,
    _must_revise,
    _normalize_stage_three_result,
    _stage_envelope,
    _stage_four_needs_review,
    _stage_three_requirements,
    _stage_timeout,
    _validate_stage_result,
)
from focus.agents.commitment.tracing import emit_commitment_trace


def _human_payload(stage: int, state: dict[str, Any], error: str = "") -> dict[str, Any]:
    draft = state.get("artifacts", {}).get(str(stage))
    decisions = (
        ["revise"]
        if _must_revise(stage, draft)
        else ["approve", "revise"]
    )
    if not error and isinstance(draft, dict):
        error = str(draft.get("feedback", ""))
    revise_label = (
        "重试或修订"
        if isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
        else "解决矛盾"
        if stage == 2 and decisions == ["revise"]
        else "补充引用"
        if stage == 4 and decisions == ["revise"]
        else "提出修订"
    )
    return {
        "type": "commitment_review",
        "stage": stage,
        "draft": draft,
        "allowed_decisions": decisions,
        "revise_label": revise_label,
        "error": error,
    }


def _tool_message_id(tool_call_id: str) -> str:
    return f"{tool_call_id}-result"


def _replace_stage_tool_message(
    state: dict[str, Any],
    stage: int,
    content: str,
) -> ToolMessage:
    for message in reversed(state.get("messages", [])):
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(str(message.content))
        except (TypeError, ValueError):
            continue
        if int(payload.get("stage", 0) or 0) != stage:
            continue
        return ToolMessage(
            content=content,
            tool_call_id=message.tool_call_id,
            id=message.id or _tool_message_id(message.tool_call_id),
        )
    raise ValueError(f"第{stage}步缺少可更新的 ToolMessage")


async def _review_human_revision(
    response: dict[str, Any],
    stage: int,
    state: dict[str, Any],
    delegator: ReviewedDelegator,
) -> tuple[Any | None, str]:
    replacement = response.get("replacement")
    feedback = str(response.get("feedback", "")).strip()
    if replacement is not None:
        if not isinstance(replacement, dict):
            return None, "replacement 必须是对象"
        error = _validate_stage_result(
            stage,
            replacement,
            list(state.get("messages", [])),
        )
        if error:
            return None, error
        worker_output = WorkerOutput(result=replacement)
        if stage == 3:
            worker_output = _normalize_stage_three_result(
                worker_output,
                _stage_three_requirements(list(state.get("messages", []))),
            )
        try:
            review = await asyncio.wait_for(
                delegator._evaluator(
                    _stage_envelope(stage, state, feedback),
                    worker_output,
                    list(state.get("messages", [])),
                    1,
                ),
                timeout=_stage_timeout(stage),
            )
        except TimeoutError:
            return None, f"阶段{stage} replacement 审核超时，请重试"
        return (
            (replacement, "")
            if review.approved
            else (None, review.feedback or "Evaluator 拒绝 replacement")
        )
    if feedback:
        try:
            output, error = await asyncio.wait_for(
                delegator.run(
                    _stage_envelope(stage, state, feedback),
                    list(state.get("messages", [])),
                ),
                timeout=_stage_timeout(stage),
            )
        except TimeoutError:
            return None, f"阶段{stage}执行超时，请重试或提交 replacement"
        return (output.result, "") if output else (None, error)
    return None, "revise 必须提供 replacement 或 feedback"


async def _human_review(
    state: CommitmentState,
    delegator: ReviewedDelegator,
) -> dict[str, Any]:
    stage = int(state.get("awaiting_human") or 0)
    if not stage:
        return {}
    artifacts = dict(state.get("artifacts", {}))
    error = ""
    while True:
        emit_commitment_trace(
            actor="supervisor",
            event="awaiting_human",
            title="Supervisor 等待人工确认",
            status="waiting",
            stage=stage,
            payload=_human_payload(stage, dict(state), error),
        )
        response = interrupt(_human_payload(stage, dict(state), error))
        if not isinstance(response, dict):
            error = "resume 必须是对象"
            continue
        decision = response.get("decision")
        if decision == "approve":
            if _must_revise(stage, artifacts.get(str(stage))):
                error = f"第{stage}步仍需修订，不能直接批准"
                continue
            if stage == 5 and _contains_unresolved_versions(artifacts.get("5")):
                error = "技术版本仍有 unresolved，必须先 revise"
                continue
            emit_commitment_trace(
                actor="human",
                event="approved",
                title="人工已批准阶段结果",
                status="approved",
                stage=stage,
            )
            artifact_ref = None
            updates: dict[str, Any] = {"awaiting_human": None}
            if stage == 7:
                emit_commitment_trace(
                    actor="system",
                    event="artifact_started",
                    title="正在写入人工确认的任务合同",
                    status="running",
                    stage=7,
                )
                try:
                    contract, artifact_ref = _write_contract(
                        str(state.get("thread_id", "")),
                        artifacts["7"],
                        str(state.get("workspace", "")),
                    )
                except (OSError, ValueError) as exc:
                    error = f"合同写入失败: {exc}"
                    continue
                updates["task_contract"] = contract
                emit_commitment_trace(
                    actor="system",
                    event="artifact_completed",
                    title="人工确认的任务合同已写入",
                    status="completed",
                    stage=7,
                    payload={"artifact_ref": artifact_ref},
                )
            updates["messages"] = [
                _replace_stage_tool_message(
                    dict(state),
                    stage,
                    _json(
                        {
                            "status": "approved",
                            "stage": stage,
                            "result": artifacts.get(str(stage)),
                            "artifact_ref": artifact_ref,
                        }
                    ),
                )
            ]
            return updates
        if decision == "revise":
            emit_commitment_trace(
                actor="human",
                event="revision_submitted",
                title="人工已提交修订",
                status="running",
                stage=stage,
                payload=response,
            )
            revised, error = await _review_human_revision(
                response,
                stage,
                dict(state),
                delegator,
            )
            if revised is None:
                continue
            artifacts[str(stage)] = revised
            knowledge_files = list(state.get("knowledge_files", []))
            artifact_ref = None
            if stage == 6:
                knowledge_files = _write_knowledge(revised, str(state.get("workspace", "")))
                artifact_ref = ",".join(knowledge_files)
            emit_commitment_trace(
                actor="supervisor",
                event="revision_accepted",
                title="修订结果已通过审核",
                status="completed",
                stage=stage,
                payload={"result": revised},
            )
            return {
                "stage": stage,
                "artifacts": artifacts,
                "knowledge_files": knowledge_files,
                "messages": [
                    _replace_stage_tool_message(
                        dict(state),
                        stage,
                        _json(
                            {
                                "status": "revised",
                                "stage": stage,
                                "result": revised,
                                "artifact_ref": artifact_ref,
                            }
                        ),
                    )
                ],
            }
        error = "decision 只允许 approve 或 revise"


def build_delegate_with_review_tool(delegator: ReviewedDelegator) -> BaseTool:
    @tool(
        "delegate_with_review",
        args_schema=TaskEnvelope,
        description="执行承诺层当前阶段，经 Worker 和 Evaluator 审核后推进状态。",
    )
    async def delegate_with_review(
        stage: int,
        instruction: str,
        context: dict[str, Any],
        acceptance_criteria: list[str],
        runtime: ToolRuntime,
    ) -> Command:
        state = dict(runtime.state)
        artifacts = dict(state.get("artifacts", {}))

        expected = int(state.get("stage", 0)) + 1
        if stage != expected:
            emit_commitment_trace(
                actor="supervisor",
                event="invalid_stage",
                title="Supervisor 拒绝非法阶段",
                status="rejected",
                stage=stage,
                payload={"expected": expected, "received": stage},
            )
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=_json(
                                {
                                    "status": "invalid_stage",
                                    "expected": expected,
                                    "received": stage,
                                }
                            ),
                            tool_call_id=runtime.tool_call_id,
                            id=_tool_message_id(runtime.tool_call_id),
                        )
                    ]
                }
            )

        envelope = TaskEnvelope(
            stage=stage,
            instruction=instruction,
            context=context,
            acceptance_criteria=acceptance_criteria,
        )

        artifact_ref = None
        if stage <= 7:
            try:
                output, error = await asyncio.wait_for(
                    delegator.run(
                        envelope,
                        list(state.get("messages", [])),
                    ),
                    timeout=_stage_timeout(stage),
                )
            except TimeoutError:
                output = None
                error = f"阶段{stage}执行超时，请重试或提交 replacement"
            if output is None:
                result = {
                    "status": "reviewed_failed",
                    "feedback": error,
                }
                artifacts[str(stage)] = result
                emit_commitment_trace(
                    actor="supervisor",
                    event="needs_human",
                    title="阶段未通过，转交人工处理",
                    status="waiting",
                    stage=stage,
                    detail=error,
                    payload=result,
                )
                return Command(
                    update={
                        "artifacts": artifacts,
                        "awaiting_human": stage,
                        "messages": [
                            ToolMessage(
                                content=_json(
                                    {
                                        "status": "reviewed_failed",
                                        "stage": stage,
                                        "result": result,
                                        "artifact_ref": None,
                                    }
                                ),
                                tool_call_id=runtime.tool_call_id,
                                id=_tool_message_id(runtime.tool_call_id),
                            )
                        ]
                    }
                )
            result = output.result
            artifact_ref = output.artifact_ref
        elif stage == 8:
            result = {"task_contract": state.get("task_contract", "")}
        else:
            result = {
                "final_message": _build_final_message(
                    state.get("task_contract", ""),
                    list(state.get("knowledge_files", [])),
                    str(state.get("workspace", "")),
                )
            }

        updates: dict[str, Any] = {"stage": stage}
        artifacts[str(stage)] = result
        updates["artifacts"] = artifacts

        if stage == 6:
            emit_commitment_trace(
                actor="system",
                event="artifact_started",
                title="正在写入理论知识文件",
                status="running",
                stage=stage,
            )
            updates["knowledge_files"] = _write_knowledge(
                result, str(state.get("workspace", ""))
            )
            artifact_ref = ",".join(updates["knowledge_files"])
            emit_commitment_trace(
                actor="system",
                event="artifact_completed",
                title="理论知识文件已写入",
                status="completed",
                stage=stage,
                payload={"artifact_ref": updates["knowledge_files"]},
            )
        elif stage == 8:
            updates["task_contract"] = state.get("task_contract", "")
        elif stage == 9:
            updates["final_message"] = result["final_message"]

        if (
            stage in _HUMAN_REVIEW_STAGES
            or (stage == 2 and _has_open_conflicts(result))
            or (stage == 4 and _stage_four_needs_review(result))
        ):
            updates["awaiting_human"] = stage

        emit_commitment_trace(
            actor="supervisor",
            event="stage_completed",
            title="Supervisor 已接收阶段结果",
            status="waiting" if updates.get("awaiting_human") else "completed",
            stage=stage,
            payload={
                "result": result,
                "artifact_ref": artifact_ref,
                "next_stage": stage + 1 if stage < 9 else None,
            },
        )

        updates["messages"] = [
            ToolMessage(
                content=_json(
                    {
                        "status": "approved",
                        "stage": stage,
                        "result": result,
                        "artifact_ref": artifact_ref,
                    }
                ),
                tool_call_id=runtime.tool_call_id,
                id=_tool_message_id(runtime.tool_call_id),
            )
        ]
        return Command(update=updates)

    return delegate_with_review


def _prepare_supervisor_call(state: CommitmentState) -> dict[str, Any]:
    stage = int(state.get("stage", 0)) + 1
    envelope = _stage_envelope(stage, dict(state))
    emit_commitment_trace(
        actor="supervisor",
        event="stage_dispatched",
        title="Supervisor 已委派阶段任务",
        status="running",
        stage=stage,
        detail=envelope.instruction,
        payload={"task_envelope": envelope.model_dump()},
    )
    return {
        "messages": [
            AIMessage(
                content=(
                    f"第 {stage} 步：{envelope.instruction}\n"
                    f"验收条件：{'；'.join(envelope.acceptance_criteria)}\n"
                    "调用 delegate_with_review，由 Evaluator 决定通过或在工具内部重试。"
                ),
                tool_calls=[
                    {
                        "name": "delegate_with_review",
                        "args": envelope.model_dump(),
                        "id": f"commitment-stage-{stage}-{len(state.get('messages', []))}",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }


def _route_supervisor(state: CommitmentState) -> str:
    if state.get("awaiting_human"):
        return "human_review"
    if int(state.get("stage", 0)) >= 9:
        return END
    messages = state.get("messages", [])
    if messages and isinstance(messages[-1], ToolMessage):
        if messages[-1].status == "error":
            return END
        try:
            if json.loads(str(messages[-1].content)).get("status") == "reviewed_failed":
                return END
        except (TypeError, ValueError):
            pass
    return "prepare_call"


def _build_supervisor(delegator: ReviewedDelegator):
    async def human_review(state: CommitmentState) -> dict[str, Any]:
        return await _human_review(state, delegator)

    builder = StateGraph(CommitmentState)
    builder.add_node("prepare_call", _prepare_supervisor_call)
    builder.add_node("human_review", human_review)
    builder.add_node(
        "delegate_with_review",
        ToolNode(
            [build_delegate_with_review_tool(delegator)],
            handle_tool_errors=False,
        ),
    )
    builder.add_edge(START, "prepare_call")
    builder.add_edge("prepare_call", "delegate_with_review")
    builder.add_conditional_edges("delegate_with_review", _route_supervisor)
    builder.add_conditional_edges("human_review", _route_supervisor)
    return builder.compile()
