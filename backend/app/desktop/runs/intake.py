"""主运行输入受理：校验引用后幂等派发普通 Main Run。

输入为已验证的运行请求及可选 HTTP 幂等身份；输出为原有受理或 Run 响应。
工作流保持引用授权、持久化幂等查询及派发通知的顺序，
不持有 HTTP Request 或应用状态。示例：MainRunIntake(desktop).submit(task_id, body)。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from backend.app.desktop.api.schemas import MainRunCreate
from backend.app.desktop.conversations.message_reference import (
    validate_message_reference,
)
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.materials import RunMaterialRequest


class MainRunIntakePorts(Protocol):
    contexts: Any

    async def run_by_idempotency(self, idempotency_key: str) -> dict | None: ...

    async def start_main_run(self, **kwargs: Any) -> PreparedRun: ...

    def notify_run_dispatch(self) -> None: ...


class MainRunIntake:
    def __init__(self, ports: MainRunIntakePorts) -> None:
        self._ports = ports

    async def submit(
        self, task_id: str, body: MainRunCreate, idempotency_header: str | None = None
    ) -> dict:
        ports = self._ports
        request_identity = (
            body.idempotency_key or idempotency_header or uuid.uuid4().hex
        )
        await validate_message_reference(ports, task_id, body.message_reference)
        idempotency_key = f"direct-user:{request_identity}"
        existing = await ports.run_by_idempotency(idempotency_key)
        if existing is not None:
            ports.notify_run_dispatch()
            return existing
        message_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"focus:direct-user-message:{request_identity}"
        ).hex
        prepared = await ports.start_main_run(
            task_id=task_id,
            message=body.message,
            model_name=body.model_name,
            reasoning_effort=body.reasoning_effort,
            message_reference=body.message_reference.model_dump()
            if body.message_reference
            else None,
            permissions=body.permissions,
            skills=body.skills,
            memory_ids=body.memory_ids,
            material_inputs=[
                RunMaterialRequest(material_id=item.material_id, note=item.note)
                for item in body.material_inputs
            ]
            if body.material_inputs is not None
            else None,
            attached_material_ids=body.attached_material_ids,
            access_mode=body.access_mode,
            run_identity={
                "message_id": message_id,
                "origin": "direct_user",
                "idempotency_key": idempotency_key,
            },
        )
        ports.notify_run_dispatch()
        return prepared.payload
