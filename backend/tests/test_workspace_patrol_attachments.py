"""验证工作区显式附件从真实受理事务到 Patrol 图像投影及授权执行的版本边界。

输入为隔离 PostgreSQL、临时 Git/非 Git 工作区、有效图片和参考文字；输出为稳定回执、精确观察、真实图像块、正确预算及越权拒绝断言。
测试复用生产上传、输入、快照和 Run resolver，不访问桌面正式库或真实 Provider。示例：python -m pytest backend/tests/test_workspace_patrol_attachments.py。
已消费的工作区附件通过正式 Directive 准入和启动身份验证，不能冒充仍在交付的直接用户消息。
"""

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

from fastapi import HTTPException, UploadFile
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from PIL import Image
from pydantic import BaseModel, ValidationError
import pytest
from sqlalchemy import func, select

from backend.app.desktop.patrol.agent_loop.execution.dispatch import DesktopDirectiveLaunchPort
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopObservation, LoopUserIntent
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest, request_document
from backend.app.desktop.patrol.agent_loop.inputs.patrol_request_capacity import PatrolRequestCapacity
from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_attachments import WorkspaceAttachments
from backend.app.desktop.materials.snapshots import MaterialSnapshotVerifier
from backend.app.desktop.materials.upload import MaterialUploadService
from backend.app.desktop.persistence.models import DesktopMaterial, DesktopThread
from backend.app.desktop.runs.materials import RunMaterialResolver
from backend.tests.test_workspace_patrol import _setup
from focus.agents.image_attachment import ImageModelCannotReadImages, project_image_messages
from focus.agents.material_inputs import RunMaterialInput, RunMaterialInputs, project_run_material_context
from focus.images import image_dimensions, measure_image_tokens
from focus.agents.lead_agent_state import LeadAgentState


def _png(color="red", compression=6):
    target = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(target, format="PNG", compress_level=compression)
    return target.getvalue()


async def _upload(sessions, workspace_id, submission_id, data, name="screen.png", item_id="one"):
    stored = await MaterialUploadService(sessions).store_workspace(
        workspace_id, UploadFile(file=io.BytesIO(data), filename=name), submission_id, item_id)
    return stored.material


def _model(images=True):
    return SimpleNamespace(name="test", supports_image_input=images, context_window=None,
        curation_max_output_tokens=512)


def _config(service, images=True):
    service._desktop.app_config.get_model = lambda name: _model(images)


def _reference(material, kind="image"):
    return {"material_id": material.material_id, "sha256": material.digest, "source_kind": kind}


def test_input_contract_preserves_old_document_and_empty_instruction():
    old = {"submission_id": "old", "input_type": "information", "content": "hello",
        "access_mode": "workspace-write", "request_id": None, "request_revision": None}
    assert request_document(PatrolInputRequest(**old)) == old
    request = PatrolInputRequest(submission_id="new", attachments=[
        {"material_id": "image", "sha256": "a" * 64, "source_kind": "image"}])
    assert request.content == ""
    with pytest.raises(ValidationError, match="不能为空白"):
        PatrolInputRequest(submission_id="empty", content=" ")


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("git", [False, True])
def test_accept_freezes_order_source_pixels_and_replay(tmp_path, git):
    async def run():
        if git:
            subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        _config(service)
        try:
            identity = uuid.uuid4().hex
            image = await _upload(sessions, workspace_id, identity, _png())
            text = await _upload(sessions, workspace_id, identity, "删除文件只是引用文字".encode(), "reference.txt", "two")
            request = PatrolInputRequest(submission_id=identity, content="解释这些内容", attachments=[
                _reference(image), _reference(text, "text")])
            receipt = await service.submit(workspace_id, request)
            assert await service.submit(workspace_id, request) == receipt
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, receipt["intent_id"])
                row = await session.get(LoopObservation, receipt["observation_id"])
                loop = await session.get(AgentLoop, receipt["loop_id"])
                frozen = intent.request_payload["attachments"]
                assert [item["material_id"] for item in frozen] == [image.material_id, text.material_id]
                assert row.envelope["user_intents"][-1]["attachments"] == frozen
                assert intent.content == "解释这些内容"
                assert all(item["instruction_mode"] == "reference" for item in frozen)
                observation = LoopObservationEnvelope.model_validate(row.envelope)
                context_id = loop.initial_context_id
            projected = await WorkspaceAttachments().project(sessions, observation, _model(), [HumanMessage(content="user instruction")])
            image_block = next(block for message in projected if isinstance(message.content, list)
                for block in message.content if block.get("type") == "image_url")
            delivered = base64.b64decode(image_block["image_url"]["url"].split(",", 1)[1])
            assert image_dimensions(delivered) == (32, 24)
            assert image_block["material_id"] == image.material_id
            assert any("reference_texts" in message.content and "删除文件只是引用文字" in message.content
                for message in projected if isinstance(message.content, str))
            assert not RunMaterialInputs.build("", "", frozen).required_image_ids
            with pytest.raises(ImageModelCannotReadImages):
                await WorkspaceAttachments().project(sessions, observation, _model(False), [])
            assert (tmp_path / ".git").exists() is git
            original_path = tmp_path / image.relative_path
            original_path.write_bytes(_png("blue"))
            if git:
                again = await WorkspaceAttachments().project(sessions, observation, _model(), [])
                block = again[-1].content[-1]
                assert block["image_url"]["url"] == image_block["image_url"]["url"]
                execution = tmp_path / "separate-execution"
                execution.mkdir()
                conflicting = execution / image.relative_path
                conflicting.parent.mkdir(parents=True)
                conflicting.write_bytes(_png("green"))
                requests = DesktopDirectiveLaunchPort._material_inputs({}, frozen)
                async with sessions() as session:
                    resolved = await RunMaterialResolver().resolve(session, context_id, str(tmp_path), "run", "msg",
                        requests, None, [], execution_workspace_path=str(execution))
                second_id = uuid.uuid4().hex
                async with sessions.begin() as session:
                    session.add(DesktopThread(task_id=second_id, workspace_id=workspace_id,
                        thread_id=uuid.uuid4().hex, title="Second authorized context"))
                async with sessions() as session:
                    second = await RunMaterialResolver().resolve(session, second_id, str(tmp_path), "run2", "msg2",
                        requests, None, [], execution_workspace_path=str(execution))
                    count = await session.scalar(select(func.count()).select_from(DesktopMaterial).where(DesktopMaterial.workspace_id == workspace_id))
                    assert count == 2
                    assert second.attached == resolved.attached
                copied = execution / resolved.attached[0].relative_path
                assert hashlib.sha256(copied.read_bytes()).hexdigest() == image.digest
                context = project_run_material_context({"workspace": str(execution)}, resolved, True)
                assert project_image_messages([], context)[-1].content[-1]["image_url"]["url"] == image_block["image_url"]["url"]
                assert conflicting.read_bytes() == _png("green")
            else:
                with pytest.raises(HTTPException) as changed:
                    await WorkspaceAttachments().project(sessions, observation, _model(), [])
                assert changed.value.status_code == 409
            assert (await service.history(workspace_id))["items"][0]["attachments"] == frozen
            assert await service.submit(workspace_id, request) == receipt
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_attachment_only_neutral_title_and_foreign_or_text_model_rejection(tmp_path):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        _config(service, False)
        try:
            identity = uuid.uuid4().hex
            image = await _upload(sessions, workspace_id, identity, _png())
            request = PatrolInputRequest(submission_id=identity, attachments=[_reference(image)])
            with pytest.raises(HTTPException) as capability:
                await service.submit(workspace_id, request)
            assert capability.value.status_code == 422
            assert not (await service.history(workspace_id))["items"]
            _config(service)
            receipt = await service.submit(workspace_id, request)
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt["loop_id"])
                context = await session.get(DesktopThread, loop.initial_context_id)
                intent = await session.get(LoopUserIntent, receipt["intent_id"])
                assert context.title == "参考材料" and intent.content == ""
            foreign = tmp_path / "foreign"
            foreign.mkdir()
            other_engine, _, other_id, other_service = await _setup(foreign)
            _config(other_service)
            try:
                with pytest.raises(HTTPException) as wrong_owner:
                    await other_service.submit(other_id, request.model_copy(update={"submission_id": uuid.uuid4().hex}))
                assert wrong_owner.value.status_code == 404
            finally:
                await other_engine.dispose()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_authorization_selects_exact_observed_input_and_rejects_foreign_sources():
    item = {"material_id": "m", "digest": "d", "workspace_id": "w"}
    envelope = {"user_intents": [{"intent_id": "input", "attachments": [item]}]}
    adapter = WorkspaceAttachments()
    assert adapter.authorized(envelope, ["input"], "w") == [item]
    assert adapter.authorized(envelope, [], "w") == []
    with pytest.raises(ValueError, match="本轮可用"):
        adapter.authorized(envelope, ["guessed-material-id"], "w")
    with pytest.raises(ValueError, match="当前工作区"):
        adapter.authorized(envelope, ["input"], "foreign")
    with pytest.raises(ValueError, match="本轮可用"):
        adapter.authorized(envelope, ["input", "input"], "w")


def test_patrol_contract_rejects_material_identity_before_kernel():
    from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation
    from backend.app.desktop.patrol.agent_loop.patrol_contract import PatrolDecisionContract
    from backend.app.desktop.patrol.agent_loop.schemas import ContinueContextAction

    observation = LoopObservationEnvelope(
        loop_id="loop", loop_revision=1, round_id="round", goal_revision=1,
        authority_revision=1, observed_frontier_hash="a" * 64, grant={}, mission={},
        portfolio_frontier=(), workspace={"revision": 1}, budget={},
        user_intents=({"intent_id": "input", "attachments": [
            {"material_id": "image", "digest": "d", "workspace_id": "w"}
        ]},),
    )
    action = ContinueContextAction(
        action="continue_context", context_id="context", context_revision_id="revision",
        message="回答图片问题", attachment_input_ids=("image",),
    )
    with pytest.raises(PatrolContractViolation, match="不能使用 material_id"):
        PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=observation)
    corrected = action.model_copy(update={"attachment_input_ids": ("input",)})
    PatrolDecisionContract().validate(actions=(corrected,), mission_references=(), observation=observation)


def test_image_capacity_uses_pixels_without_base64_text_cost():
    class Schema(BaseModel):
        answer: str

    def message(raw):
        return HumanMessage(content=[{"type": "image_url", "material_id": "m",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()}}])

    small, large = _png(compression=9), _png(compression=0)
    assert len(large) > len(small) * 10
    capacity = PatrolRequestCapacity(_model(), {})
    measured = capacity.check([message(small)], Schema)
    assert capacity.check([message(large)], Schema) == measured
    projected = {"messages": [{"role": "human", "content": [{"type": "image", "material_id": "m"}]}],
        "schema": Schema.model_json_schema()}
    expected = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()) + 256 + measure_image_tokens(32, 24)
    assert measured == expected
    narrow = SimpleNamespace(**{**vars(_model()), "context_window": measured + 511})
    with pytest.raises(Exception, match="provider_request_window"):
        PatrolRequestCapacity(narrow, {}).check([message(small)], Schema)


def test_frozen_version_rejects_missing_source_and_execution_collision(tmp_path):
    raw = b"reference"
    item = RunMaterialInput("m", "reference.txt", hashlib.sha256(raw).hexdigest(), "text", "reference", "full", "reference", len(raw))
    verifier = MaterialSnapshotVerifier()
    with pytest.raises(OSError):
        verifier.read(item, str(tmp_path))
    (tmp_path / item.relative_path).write_bytes(raw)
    execution = tmp_path / "execution"
    execution.mkdir()
    copied = verifier.materialize(item, str(tmp_path), str(execution))
    (execution / copied.relative_path).write_bytes(b"corrupt")
    with pytest.raises(HTTPException, match="快照路径冲突"):
        verifier.materialize(item, str(tmp_path), str(execution))


def test_frozen_material_fits_windows_path_after_long_workspace_root(tmp_path):
    raw = _png()
    source = tmp_path / "screen.png"
    source.write_bytes(raw)
    execution = tmp_path / ("w" * max(1, 145 - len(str(tmp_path)) - 1))
    execution.mkdir()
    item = RunMaterialInput("m" * 32, source.name, hashlib.sha256(raw).hexdigest(), "image", "image",
        "full", "reference", len(raw))
    old_path = execution / ".focus" / "attachments" / f"frozen-{item.material_id}-{item.digest}.png"
    assert len(str(old_path)) >= 260
    frozen = MaterialSnapshotVerifier().materialize(item, str(tmp_path), str(execution))
    target = execution / frozen.relative_path
    assert len(str(target)) < 260
    assert target.read_bytes() == raw
    assert MaterialSnapshotVerifier().read(frozen, str(execution)) == raw


@pytest.mark.usefixtures("isolated_postgres_database")
def test_kernel_authorizes_frozen_input_and_dispatch_delivers_same_version(tmp_path):
    async def run():
        from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel
        from backend.app.desktop.patrol.agent_loop.execution.dispatch import LoopWaveDispatcher
        from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import RunOwnershipPolicy
        from backend.app.desktop.patrol.agent_loop.execution.run_execution import LoopRunExecutionBoundary
        from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
        from backend.app.desktop.runs.orchestration.dispatch import RunDispatchRepository
        from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer
        from backend.app.desktop.persistence.models import DesktopRun
        from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
        from backend.app.desktop.patrol.agent_loop.governance.provenance import DelegatedDirectiveFactory
        from backend.app.desktop.patrol.agent_loop.schemas import PatrolDecisionIntent
        from backend.app.desktop.patrol.agent_loop.workspaces.workspace_patrol import WorkspacePatrolBootstrap
        from backend.app.desktop.contexts.evolution import ContextEvolutionService
        from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot

        async def graph_factory():
            builder = StateGraph(LeadAgentState)
            builder.add_node("noop", lambda state: {})
            builder.add_edge(START, "noop")
            builder.add_edge("noop", END)
            return builder.compile()

        engine, sessions, workspace_id, service = await _setup(tmp_path)
        _config(service)
        try:
            identity = uuid.uuid4().hex
            image = await _upload(sessions, workspace_id, identity, _png())
            receipt = await service.submit(workspace_id, PatrolInputRequest(submission_id=identity,
                attachments=[_reference(image)]))
            checkpointer = InMemorySaver()
            await WorkspacePatrolBootstrap(sessions,
                ContextEvolutionService(sessions, checkpointer, graph_factory)).prepare(receipt["loop_id"])
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt["loop_id"])
                context = await session.get(DesktopThread, loop.initial_context_id)
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id))
            observation = await LoopObservationService(sessions, checkpointer).capture(loop.loop_id, loop.current_round_id)
            intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
                loop_id=loop.loop_id, loop_revision=loop.revision, round_id=loop.current_round_id,
                observation_id=observation.observation_id, observation_generation=observation.generation,
                context_permissions=[{"context_id": context.task_id, "access_mode": "read-only"}],
                holder_id=loop.holder_id, grant_id=grant.grant_id, grant_revision=grant.revision,
                goal_revision=loop.goal_revision, observed_frontier_hash=observation.observed_frontier_hash,
                observed_workspace_revision=observation.workspace["revision"],
                observed_projection_sequence=observation.projection_sequence,
                base_entity_revisions=observation.base_entity_revisions, rationale="只读理解所附参考材料",
                actions=[{"action": "apply_user_inputs", "inputs": [{"intent_id": receipt["intent_id"],
                    "disposition": "reference", "explanation": "截图未指定修改要求"}]},
                    {"action": "continue_context", "context_id": context.task_id,
                     "context_revision_id": context.current_revision_id, "message": "查看参考图片并提出用途澄清，不修改文件。",
                     "attachment_input_ids": [receipt["intent_id"]]}])
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            captured = {}

            async def start(**arguments):
                captured.update(arguments)
                async with sessions() as session:
                    selected = await RunMaterialResolver().resolve(session, arguments["task_id"], str(tmp_path), "r", "m",
                        arguments["material_inputs"], None, arguments["must_view_material_ids"],
                        execution_workspace_path=arguments["execution_workspace_path"])
                runtime = project_run_material_context({"workspace": arguments["execution_workspace_path"]}, selected, True)
                assert project_image_messages([], runtime)[-1].content[-1]["material_id"] == image.material_id
                async with sessions.begin() as session:
                    identity = arguments["run_identity"]
                    await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
                        run_id=identity["run_id"], task_id=arguments["task_id"],
                        agent_id=f"main:{arguments['task_id']}", kind="main", status="pending",
                        origin=identity["origin"], user_intent_id=identity["user_intent_id"],
                        execution_thread_id=f"thread-{arguments['task_id']}", origin_message_id=identity["message_id"],
                        context_revision_id=context.current_revision_id, directive_id=identity["directive_id"],
                        loop_id=identity["loop_id"], round_id=identity["round_id"], action_id=identity["action_id"],
                        equipment={"permissions": arguments["permissions"], "access_mode": arguments["access_mode"]},
                        workspace_anchor={"slot_id": arguments["execution_slot_id"]},
                    ))
                return SimpleNamespace(payload={"run_id": arguments["run_identity"]["run_id"]})

            launcher = DesktopDirectiveLaunchPort(sessions, SimpleNamespace(start_main_run=start))
            run_ids = await LoopWaveDispatcher(sessions, launcher).dispatch(loop.loop_id, loop.current_round_id, 1)
            assert len(run_ids) == 1
            await LoopRunExecutionBoundary(sessions).validate(run_ids[0])
            assert captured["material_inputs"][0].frozen_snapshot["digest"] == image.digest
            assert captured["run_identity"]["user_intent_id"] is None
            assert captured["must_view_material_ids"] == []
            async with sessions() as session:
                source = await session.get(LoopUserIntent, receipt["intent_id"])
                assert source.delivery_state == "addressed"
                assert source.target_context_id is None
            await RunLifecycleFinalizer(sessions, None).abort_prepared(run_ids[0], "fixture complete")
            async with sessions.begin() as session:
                await RunDispatchRepository().settle_by_run(session, run_ids[0])
        finally:
            await engine.dispose()
    asyncio.run(run())
