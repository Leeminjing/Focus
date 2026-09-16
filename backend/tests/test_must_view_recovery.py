"""本文件验证必看报告恢复探测、载荷校验和统一运行材料上下文投影。

输入为内存 checkpoint tuple、最近主运行状态、旧/新 equipment 与 retry/cancel 载荷；输出为
processing/resumable/orphaned 状态、非法载荷拒绝，以及初始运行和任意主运行恢复使用完全相同的
材料投影并保留被中断 Revision 的执行身份。具体工作流断言顺序/备注、图片、required、模型能力、
execution thread/namespace 与 revision id，不启动模型或后台 worker。

示例：python -m pytest backend/tests/test_must_view_recovery.py。
"""

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from backend.app.desktop.models import DesktopRun
from backend.app.desktop.must_view_recovery import (
    must_view_recovery_payload,
    validate_must_view_resume,
)
from backend.app.desktop.service import DesktopService
from focus.agents.material_inputs import RunMaterialInputs


class Checkpointer:
    def __init__(self, payload):
        self.payload = payload

    async def aget_tuple(self, _config):
        if self.payload is None:
            return None
        interrupt = SimpleNamespace(value=self.payload)
        return SimpleNamespace(pending_writes=[("task", "__interrupt__", [interrupt])])


class ScalarSession:
    def __init__(self, latest):
        self.latest = latest

    async def scalar(self, _query):
        return self.latest


class CommitSession:
    def __init__(self, latest=None):
        self.added = []
        self.latest = latest

    def add(self, value):
        self.added.append(value)

    async def scalar(self, _query):
        return self.latest

    async def commit(self):
        return None


def _payload():
    return {
        "type": "must_view_report",
        "missing": ["m1"],
        "unread": [],
        "materials": [{"material_id": "m1", "relative_path": "shot.png", "reason": "missing"}],
        "actions": ["retry", "cancel"],
    }


@pytest.mark.parametrize(
    ("latest", "expected"),
    [
        (SimpleNamespace(status="running"), "processing"),
        (SimpleNamespace(status="interrupted"), "resumable"),
        (SimpleNamespace(status="error"), "orphaned"),
        (None, "orphaned"),
    ],
)
def test_recovery_status_follows_latest_main_run(latest, expected) -> None:
    task = SimpleNamespace(thread_id="thread", task_id="task")
    result = asyncio.run(
        must_view_recovery_payload(ScalarSession(latest), task, Checkpointer(_payload()))
    )
    assert result["status"] == expected
    assert result["request"] == _payload()


def test_recovery_ignores_other_interrupts_and_validates_decisions() -> None:
    task = SimpleNamespace(thread_id="thread", task_id="task")
    assert asyncio.run(
        must_view_recovery_payload(
            ScalarSession(None), task, Checkpointer({"type": "compression_request"})
        )
    ) is None
    assert validate_must_view_resume(
        {"type": "must_view_report", "decision": "retry"}
    ) == {"type": "must_view_report", "decision": "retry"}
    with pytest.raises(HTTPException):
        validate_must_view_resume({"type": "must_view_report", "decision": "ignore"})
    with pytest.raises(HTTPException):
        validate_must_view_resume({"type": "compression_request", "decision": "cancel"})


def test_initial_and_resume_paths_project_identical_material_context(tmp_path) -> None:
    service = object.__new__(DesktopService)
    # 执行身份的工作区必须是绝对路径（唯一派生会拒绝相对工作区），因此用真实临时目录
    workspace_path = str(tmp_path)

    async def material_context(_task_id):
        return "", ""

    async def apply_memory(prompt, _memory_ids):
        return prompt

    service._material_context = material_context
    service._apply_memory_block = apply_memory
    service._build_agent_factory = lambda *_args: (lambda: None)
    service._model_supports_image_input = lambda _name: True
    inputs = RunMaterialInputs.build(
        "origin-run",
        "origin-message",
        [{
            "material_id": "m1",
            "relative_path": "shot.png",
            "digest": "legacy",
            "material_kind": "image",
            "delivery_mode": "image",
            "reading_mode": "full",
            "instruction_mode": "reference",
            "size_bytes": 80,
            "note": "逐像素核对",
            "source_bytes": 80,
            "model_bytes": 64,
            "model_tokens": 7,
        }],
        ["m1"],
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
    resumed = asyncio.run(
        DesktopService._prepare_main_resume(
            service,
            CommitSession(interrupted),
            task,
            workspace,
            {"type": "must_view_report", "decision": "retry"},
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
