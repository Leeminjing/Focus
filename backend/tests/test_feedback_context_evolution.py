"""本文件验证普通反馈经过 Mission、原 Context 诊断和精确编译抵达新 Context。

输入为隔离数据库、真实输入/Kernel/派发/结算组件和受控模型输出；输出为 U/F/D 原文、来源及 B checkpoint 断言。
工作流在方案 Run 内写文件并读取 V1，普通输入引用 V1 后才结束 Run，再派发诊断并按既有证据合同编译与发布 B。
示例：python -m pytest backend/tests/test_feedback_context_evolution.py -q；模型输出为 fixture，不宣称真实模型必然选择策略。
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from sqlalchemy import select

from focus.agents.lead_agent_state import LeadAgentState
from backend.tests.test_workspace_patrol import _setup
from backend.tests.test_workspace_patrol_decisions import _prepare, _decision, _use, _checkpointers
from backend.tests.loop_run_boundary_support import freeze_patrol_intent, start_loop_run
from backend.tests.test_loop_execution_ownership import _admit_run
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher, LoopRunWorkspaceBinder
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.context_expansion.mission_sections import FrozenMissionSectionCatalog
from backend.app.desktop.agent_loop.context_expansion.contracts import EvidenceRequirement, WorkContextSpec, ResolvedEvidenceItem, ResolvedEvidenceBundle, ExpansionOpportunity, SpawnContextIntent
from backend.app.desktop.agent_loop.context_expansion.compiler import DeterministicExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import DeterministicTestContextQualityService
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import FrozenEvidenceCorpusReader
from backend.app.desktop.context_curation import MultiSourceEvidence, SourceRevisionEvidence, StructuredEvidence, compile_lane
from backend.app.desktop.context_evolution import ContextEvolutionService, ContextRevisionSourceContract
from backend.app.desktop.context_evolution.schemas import ContextRevisionOriginKind
from backend.app.desktop.models import DesktopThread, DesktopRun
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer
from backend.app.desktop.execution_attempts import ToolExecutionLedger
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.domain_evidence.file_reads import FileVersionReader
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.tests.runtime_context_support import tool_runtime
from focus.tools.builtins.workspace_tools import write_file
from focus.security.context import security_context_of


async def _execute(sessions, loop_id, receipt, section, message, output, output_id, on_running=None):
    async with sessions() as session:
        loop = await session.get(AgentLoop, loop_id)
        context = await session.get(DesktopThread, loop.initial_context_id)
    actions = [{"action": "apply_user_inputs", "inputs": [_use(receipt)], "changes": [
        {"intent_id": receipt["intent_id"], "section": section, "quote": receipt["content"]}]},
        {"action": "continue_context", "context_id": context.task_id,
         "context_revision_id": context.current_revision_id, "message": message}]
    intent = await freeze_patrol_intent(sessions, await _decision(sessions, loop_id, actions))
    committed = await LoopKernel(sessions).commit(intent)
    assert committed.status == "committed", committed.reason
    fixture = {"context_id": context.task_id, "revision_id": context.current_revision_id, "loop_id": loop_id, "round_id": intent.round_id}
    async def launch(directive, message, slot):
        return await _admit_run(sessions, fixture, directive.directive_id)
    runs = await LoopWaveDispatcher(sessions, launch).dispatch(loop_id, intent.round_id, 1)
    assert len(runs) == 1
    await LoopRunWorkspaceBinder(sessions).bind(run_id=runs[0], loop_id=loop_id, body=SimpleNamespace(context={}))
    await start_loop_run(sessions, runs[0])
    async with sessions() as session:
        run = await session.get(DesktopRun, runs[0])
    if on_running:
        await on_running(run)
    checkpointer = _checkpointers[loop_id]
    graph = StateGraph(LeadAgentState)
    graph.add_node("response", lambda state: {})
    graph.add_edge(START, "response"); graph.add_edge("response", END)
    await graph.compile(checkpointer=checkpointer).ainvoke({"messages": [
        HumanMessage(id=f"input-{output_id}", content=message), AIMessage(id=output_id, content=output)]},
        {"configurable": {"thread_id": run.execution_thread_id}}, durability="sync")
    record = SimpleNamespace(run_id=run.run_id, status=SimpleNamespace(value="success"), error=None,
        model_call_count=0, prompt_input_tokens=0, prompt_output_tokens=0, prompt_cache_hit_tokens=0)
    settled = await RunLifecycleFinalizer(sessions, checkpointer).finalize(record)
    await RunOutboxConsumer(sessions).drain("evolution-test", LoopCoordinator(sessions).handle_run_settled, loop_id=loop_id)
    return settled.context_revision


async def _write_live_version(sessions, run, content, call_id):
    async with sessions() as session:
        slot = await session.get(WorkspaceSlot, run.workspace_anchor["slot_id"])
    ledger = ToolExecutionLedger(sessions, authority_validator=RunOwnershipPolicy().assert_live)
    call = {"id": call_id, "name": "write_file", "args": {"path": "plan.md", "content": content}}
    identity = uuid.uuid4().hex * 2
    assert await ledger.claim(identity, run.run_id, call) is None
    capture = await ledger.prepare_file_capture(run.run_id)
    runtime = tool_runtime(agent_id="main", task_id=run.task_id, run_id=run.run_id,
        workspace=slot.root_path, permissions=("read", "write"))
    security = replace(security_context_of(runtime.context), extras={"file_version_capture": capture})
    runtime = replace(runtime, context={**runtime.context, **security.to_runtime_context()})
    result = ToolMessage(content=write_file.func(**call["args"], runtime=runtime), tool_call_id=call_id)
    await ledger.complete(identity, result, file_capture=capture)
    assert (await ledger.claim(identity, run.run_id, call)).content == result.content
    consumer = RunOutboxConsumer(sessions)
    await consumer.drain("feedback-test", LoopCoordinator(sessions).handle_run_settled, loop_id=run.loop_id)
    assert await consumer.drain("feedback-test", LoopCoordinator(sessions).handle_run_settled, loop_id=run.loop_id) == 0
    await FactProjector(sessions, None).project_loop(run.loop_id)
    async with sessions() as session:
        assert (await session.get(DesktopRun, run.run_id)).settled_at is None
        fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == run.loop_id,
            LoopFact.fact_type == "artifact").order_by(LoopFact.occurred_at.desc()))
        version = await FileVersionReader().read(session, run.loop_id, fact.fact_id)
    assert version["after"]["text"] == content
    return {key: version[key] for key in ("fact_id", "revision", "result_key", "slot_id", "sha256", "path")}


def test_diagnosis_rebuild_preserves_feedback_without_new_evidence_type(runtime_postgres_database, tmp_path):
    async def run():
        engine, sessions, workspace, service = await _setup(tmp_path)
        requirement = "设计本地方案，保留导出功能"
        feedback = "  不满意：不要云端依赖，请保留离线能力。\n"
        diagnosis = "失败原因：上一方案引入云端依赖，违反本地要求；导出能力仍然有效。"
        obsolete = "旧方案：所有数据上传云端"
        try:
            first = await service.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content=requirement))
            loop_id = first["loop_id"]
            await _prepare(sessions, loop_id)
            correction, reference = None, None
            async def review_file(run):
                nonlocal correction, reference
                reference = await _write_live_version(sessions, run, obsolete, "file-v1")
                await _write_live_version(sessions, run, obsolete + "（更新排版）", "file-v2")
                request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content=feedback, file_refs=(reference,))
                correction = await service.submit(workspace, request)
                assert await service.submit(workspace, request) == correction
                async with sessions() as session:
                    still_running = await session.get(DesktopRun, run.run_id)
                    assert still_running.status == "running" and still_running.settled_at is None
                    assert (await FileVersionReader().read(session, loop_id, reference["fact_id"], reference["revision"]))["after"]["text"] == obsolete
                assert correction["content"] == feedback and correction["file_refs"][0] == reference
            await _execute(sessions, loop_id, first, "outcome", requirement, obsolete, "old-plan", review_file)
            diagnosis_input = "这是用户的反馈，请输出失败原因：\n" + feedback + f"\n引用 plan.md 版本 {reference['sha256']}"
            await _execute(sessions, loop_id, correction, "boundary", diagnosis_input, diagnosis, "diagnosis")
            checkpointer = _checkpointers[loop_id]
            async def graph_factory():
                graph = StateGraph(LeadAgentState)
                graph.add_node("noop", lambda state: {})
                graph.add_edge(START, "noop"); graph.add_edge("noop", END)
                return graph.compile()
            evolution = ContextEvolutionService(sessions, checkpointer, graph_factory)
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                current = await evolution.current(session, loop.initial_context_id)
                view = await evolution.read_current_display(loop.initial_context_id)
                mission_row = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop_id).order_by(LoopMissionRevision.revision.desc()))
                assert mission_row.input_sources["outcome"][0]["intent_id"] == first["intent_id"]
                assert feedback.strip() in mission_row.boundaries["required_invariants"]
                assert mission_row.input_sources["boundary:required_invariants"][0]["content"] == feedback
                mission = EffectiveMissionProjector.from_rows(structured=mission_row, legacy=None)
            catalog = FrozenMissionSectionCatalog.from_mission(loop_id, mission)
            structured = tuple(StructuredEvidence(ref=entry.ref, content=entry.content) for entry in catalog.entries)
            messages = tuple(FrozenEvidenceCorpusReader._message(current.ref, item) for item in view.messages if item.get("id") in {"diagnosis", "input-diagnosis"})
            assert len(messages) == 2 and messages[-1].content == diagnosis
            evidence = MultiSourceEvidence(sources=(SourceRevisionEvidence(source=current.ref, projection_hash=current.projection_hash or current.content_hash,
                content_hash=current.content_hash, messages=messages),), structured=structured,
                evidence_frontier=tuple(item.ref for item in structured) + tuple(item.ref for item in messages))
            requirements = tuple(EvidenceRequirement(requirement_id=f"required-{i}", role="requirement", question="哪些要求仍有效？", coverage_criterion="原文及来源") for i in range(len(structured)))
            requirements += (EvidenceRequirement(requirement_id="diagnosis", role="failure", question="为何失败？", coverage_criterion="原 Context 已发布诊断"),)
            requirements += (EvidenceRequirement(requirement_id="feedback", role="conversation", question="用户怎样纠正？", coverage_criterion="完整反馈原文"),)
            spec = WorkContextSpec.create(planner_version="fixture", objective="依据诊断重新设计本地方案", separation_reason="从有效要求和失败诊断重新构建",
                questions=("如何满足本地与导出约束？",), completion_criteria=("不依赖云端",), workspace_requirement="read_only", evidence_requirements=requirements)
            items = tuple(ResolvedEvidenceItem(requirement_id=f"required-{i}", ref=item.ref, content_hash=item.ref.content_hash, relevance_reason="有效用户要求") for i,item in enumerate(structured))
            items += tuple(ResolvedEvidenceItem(requirement_id="diagnosis" if item.ref.message_id == "diagnosis" else "feedback",
                ref=item.ref, content_hash=current.content_hash, relevance_reason="已发布诊断与原文") for item in messages)
            bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
            synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
            quality = await DeterministicTestContextQualityService().verify(spec, bundle, synthesis.dossier)
            opportunity = ExpansionOpportunity.create(loop_id=loop_id, round_id=loop.current_round_id, observation_hash="a"*64, work_spec=spec, manifest_sources=(current.ref,))
            compiled = DeterministicExpansionPlanCompiler().compile(opportunity, SpawnContextIntent(opportunity_id=opportunity.opportunity_id), bundle, dossier=synthesis.dossier, quality_assessment=quality.assessment)
            candidate = compile_lane(compiled.plan.model_dump(mode="json"), evidence)
            child_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopThread(task_id=child_id, thread_id=child_id, workspace_id=workspace, title="重做"))
                await session.flush()
                child = await evolution.stage_definition(session, child_id, candidate.authored_messages,
                    (ContextRevisionSourceContract(source=current.ref, position=0),), ContextRevisionOriginKind.CURATION, compiled.expansion_id)
            child_view = await evolution.read_current_display(child_id)
            text = "\n".join(str(item.get("content", "")) for item in child_view.messages)
            assert requirement in text and feedback in text and diagnosis in text
            assert reference["sha256"] in text
            assert obsolete not in text
            assert child.sources[0].source == current.ref
            assert child.ref.is_runnable
        finally:
            await engine.dispose()
    asyncio.run(run())
