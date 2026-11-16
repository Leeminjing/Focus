"""本文件对外提供统一运行材料上下文恢复投影测试。

输入为最近主运行状态、旧/新 equipment 与压缩恢复载荷；输出为初始运行和主运行恢复使用完全相同的
材料投影并保留被中断 Revision 的执行身份。具体工作流为通过同源准备接口，
验证顺序/备注、图片、模型能力、execution thread/namespace 与 revision id，
同时断言普通主运行不启用 Patrol 准备观察器；旧图片描述在压缩恢复和写回后仍交付像素，
不恢复必看声明；不启动模型或后台 worker。

示例：python -m pytest backend/tests/test_material_context_recovery.py。
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from focus.agents.image_attachment import project_image_messages
from focus.agents.material_inputs import RunMaterialInputs
from focus.history import HistoryPayload, content_hash
from langchain_core.messages import HumanMessage
from PIL import Image

from backend.app.desktop.composition.service import DesktopService
from backend.app.desktop.contexts.evolution import (
    ContextRevisionContract,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.persistence.models import DesktopRun


class CommitSession:
    def __init__(self, latest=None):
        self.added = []
        self.latest = latest
        self.input_owners = {}
        self.released_input_owners = []
        self.context_source = None
        if latest is not None and latest.context_revision_id:
            self.context_source = ContextRevisionContract(
                ref=ContextRevisionRef(context_id=latest.task_id, revision_id=latest.context_revision_id,
                    generation=1, execution_thread_id=latest.execution_thread_id,
                    checkpoint_ns=latest.checkpoint_ns or "", checkpoint_id=latest.context_checkpoint_id,
                    payload_mode="checkpoint"),
                history_payload=HistoryPayload(), content_hash=content_hash([]),
                projection_status="valid", origin_kind="root", created_at=datetime.now(UTC),
            )
            self.input_owners[("run", latest.run_id)] = self.context_source.ref.revision_id

    def add(self, value):
        self.added.append(value)

    async def scalar(self, _query):
        return self.latest

    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def _snapshot_persistence(monkeypatch):
    """Provide exact Context inputs for the material-only in-memory Run port."""
    async def by_id(session, token):
        assert session.context_source is not None and session.context_source.ref.revision_id == token
        return session.context_source

    async def by_ref(session, ref):
        source = await by_id(session, ref.revision_id)
        assert source.ref == ref
        return source

    async def freeze(session, ref, *, owner_kind, owner_id):
        source = await by_ref(session, ref)
        session.input_owners[(owner_kind, owner_id)] = ref.revision_id
        return source

    async def for_run(session, run, *, resume=False):
        assert not resume
        source = await by_id(session, run.context_revision_id)
        assert session.input_owners[("run", run.run_id)] == source.ref.revision_id
        return source

    async def release(session, owner_kind, owner_id):
        session.released_input_owners.append((owner_kind, owner_id))
        session.input_owners.pop((owner_kind, owner_id), None)

    for name, handler in {"get_by_id": by_id, "get_input": by_ref, "input_for_run": for_run,
                          "freeze": freeze, "release": release}.items():
        original = getattr(ContextRevisionRepository, name)

        async def invoke(repository, session, *args, _handler=handler, _original=original, **kwargs):
            if isinstance(session, CommitSession):
                return await _handler(session, *args, **kwargs)
            return await _original(repository, session, *args, **kwargs)

        monkeypatch.setattr(ContextRevisionRepository, name, invoke)


def test_initial_and_resume_paths_project_identical_material_context(tmp_path) -> None:
    service = object.__new__(DesktopService)
    service.checkpointer = None
    # 执行身份的工作区必须是绝对路径（唯一派生会拒绝相对工作区），因此用真实临时目录
    workspace_path = str(tmp_path)

    async def material_context(_task_id):
        return "", ""

    async def apply_memory(prompt, _memory_ids):
        return prompt

    service.materials._material_context = material_context
    service._apply_memory_block = apply_memory
    def build_agent_factory(*_args, preparation_observer=None):
        assert preparation_observer is None
        return lambda: None

    service.agent_factory._build_agent_factory = build_agent_factory
    service.capabilities._model_supports_image_input = lambda _name: True
    inputs = RunMaterialInputs.build(
        "origin-run",
        "origin-message",
        [{
            "material_id": "m1",
            "relative_path": "shot.png",
            "digest": "legacy",
            "material_kind": "image",
            "delivery_mode": "image",
            "size_bytes": 80,
            "note": "逐像素核对",
            "source_bytes": 80,
            "model_bytes": 64,
            "model_tokens": 7,
        }],
    )
    equipment = {
        "model_name": "vision-model",
        "skills": [],
        "skill_snapshots": [],
        "permissions": ["read"],
        **inputs.to_equipment(),
    }
    initial_run = DesktopRun(
        run_id="initial",
        task_id="task",
        agent_id="main:task",
        kind="main",
        status="pending",
        input_messages=[],
        model_name="vision-model",
    )
    initial = asyncio.run(
        DesktopService._prepare(
            service,
            initial_run,
            "thread",
            "workspace",
            workspace_path,
            [{"role": "human", "content": "look"}],
            "prompt",
            equipment,
            "",
            "main",
        )
    )
    task = SimpleNamespace(
        task_id="task",
        thread_id="thread",
        ui_state={"_main_run_equipment": equipment},
    )
    workspace = SimpleNamespace(workspace_id="workspace", path=workspace_path)
    interrupted = DesktopRun(
        run_id="interrupted",
        task_id="task",
        agent_id="main:task",
        kind="main",
        status="interrupted",
        input_messages=[],
        model_name="vision-model",
        execution_thread_id="shadow:task:revision",
        checkpoint_ns="context-revision-shadow",
        context_revision_id="revision",
        context_checkpoint_id="checkpoint",
    )
    session = CommitSession(interrupted)
    resumed = asyncio.run(
        DesktopService._prepare_main_resume(
            service,
            session,
            task,
            workspace,
            {"type": "compression", "decision": "retry"},
        )
    )
    assert initial.body.context["run_image_inputs"] == resumed.body.context["run_image_inputs"]
    assert initial.body.context["run_material_inputs"] == resumed.body.context["run_material_inputs"]
    assert resumed.body.context["run_material_inputs"]["attached"][0]["note"] == "逐像素核对"
    assert initial.body.context["model_supports_image_input"] is True
    assert resumed.body.context["model_supports_image_input"] is True
    assert resumed.thread_id == "shadow:task:revision"
    assert resumed.body.context["checkpoint_ns"] == "context-revision-shadow"
    assert resumed.payload["context_revision_id"] == "revision"
    assert session.released_input_owners == [("run", "interrupted")]
    assert ("run", "interrupted") not in session.input_owners


@pytest.mark.parametrize("equipment_source", ["run", "task"])
def test_legacy_image_survives_compression_resume_and_equipment_rewrite(tmp_path, equipment_source) -> None:
    Image.new("RGB", (8, 8), "red").save(tmp_path / "shot.png")
    image = {
        "material_id": "old-image",
        "relative_path": "shot.png",
        "source_bytes": (tmp_path / "shot.png").stat().st_size,
        "model_bytes": 64,
        "model_tokens": 7,
    }
    equipment = {
        "model_name": "vision-model",
        "skills": [],
        "skill_snapshots": [],
        "permissions": ["read"],
        "must_view_materials": [image],
    }
    service = object.__new__(DesktopService)
    service.checkpointer = None
    service.agent_factory._build_agent_factory = lambda *_args: lambda: None
    service.capabilities._model_supports_image_input = lambda _name: True
    interrupted = DesktopRun(
        run_id="interrupted",
        task_id="task",
        agent_id="main:task",
        kind="main",
        status="interrupted",
        input_messages=[],
        model_name="vision-model",
        equipment=equipment if equipment_source == "run" else None,
    )
    task = SimpleNamespace(
        task_id="task",
        thread_id="thread",
        ui_state={"_main_run_equipment": equipment if equipment_source == "task" else {}},
    )
    workspace = SimpleNamespace(workspace_id="workspace", path=str(tmp_path))
    session = CommitSession(interrupted)
    resumed = asyncio.run(DesktopService._prepare_main_resume(
        service, session, task, workspace, {"type": "compression", "decision": "retry"},
    ))

    context = resumed.body.context
    assert [item["material_id"] for item in context["run_image_inputs"]["attached"]] == ["old-image"]
    stored = session.added[-1].equipment
    assert session.released_input_owners == [("run", "interrupted")]
    assert stored["run_material_inputs"] == context["run_material_inputs"]
    assert "must_view_materials" not in stored
    assert "must_view_materials" not in context
    assert "required_ids" not in context["run_image_inputs"]
    assert "required_image_ids" not in stored["run_material_inputs"]
    assert RunMaterialInputs.from_equipment(stored).images.to_json() == context["run_image_inputs"]

    messages = project_image_messages([HumanMessage(content="look")], context)
    blocks = [block for block in messages[-1].content if block["type"] == "image_url"]
    assert len(blocks) == 1
    assert blocks[0]["material_id"] == "old-image"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
