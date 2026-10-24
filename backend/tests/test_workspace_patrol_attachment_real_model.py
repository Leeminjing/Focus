"""提供显式启用的工作区图片真实 Provider 验收。

输入为宿主已配置视觉模型、隔离数据库和临时合成界面截图；输出为受理版本、实际 HTTP 图像摘要及视觉结果断言。
流程使用生产上传、workspace intake、Patrol 附件投影和同一结构化请求调用，记录安全摘要而不输出凭据或原始请求。
示例：FOCUS_PATROL_ATTACHMENT_REAL_MODEL=1 python -m pytest backend/tests/test_workspace_patrol_attachment_real_model.py -s。
"""

import asyncio
import base64
import hashlib
import io
import json
import os
from typing import Literal
import uuid

import httpx
from langchain_core.messages import HumanMessage
from PIL import Image, ImageDraw
from pydantic import BaseModel
import pytest

from backend.app.desktop.patrol.agent_loop.models import LoopObservation
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import StructuredPatrolDecisionModel
from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_attachments import WorkspaceAttachments
from backend.tests.test_workspace_patrol import _setup
from backend.tests.test_workspace_patrol_attachments import _reference, _upload
from focus.config.app_config import get_app_config
from focus.models.factory import create_chat_model


pytestmark = [
    pytest.mark.usefixtures("isolated_postgres_database"),
    pytest.mark.skipif(os.environ.get("FOCUS_PATROL_ATTACHMENT_REAL_MODEL") != "1", reason="explicit real-provider smoke"),
]


class VisualAnswer(BaseModel):
    left_color: Literal["red", "blue", "green", "yellow"]
    right_shape: Literal["circle", "square", "triangle"]


def test_real_provider_receives_frozen_screenshot_pixels(tmp_path):
    async def run():
        config = get_app_config("config.yaml")
        selected = config.get_model(config.resolve_default_model_name())
        if not selected.supports_image_input:
            pytest.skip("当前已配置默认模型未声明视觉能力，不自动切换模型")
        picture = Image.new("RGB", (640, 360), "white")
        drawing = ImageDraw.Draw(picture)
        drawing.rectangle((70, 100, 260, 280), fill="red")
        drawing.ellipse((380, 100, 560, 280), fill="blue")
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        raw = buffer.getvalue()
        path = tmp_path / "visual-fixture.png"
        path.write_bytes(raw)
        evidence = []

        async def inspect_request(request):
            payload = json.loads(request.content)
            image_blocks = [block for message in payload.get("messages", ())
                if isinstance(message.get("content"), list) for block in message["content"]
                if block.get("type") == "image_url"]
            for block in image_blocks:
                data = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
                evidence.append({"image_sha256": hashlib.sha256(data).hexdigest(), "image_bytes": len(data)})

        engine, sessions, workspace_id, service = await _setup(tmp_path)
        service._desktop.app_config = config
        try:
            identity = uuid.uuid4().hex
            material = await _upload(sessions, workspace_id, identity, raw)
            instruction = "只根据所附图片回答左侧图形的颜色和右侧图形的形状，使用英文小写。"
            receipt = await service.submit(workspace_id, PatrolInputRequest(submission_id=identity,
                content=instruction, attachments=[_reference(material)]))
            async with sessions() as session:
                row = await session.get(LoopObservation, receipt["observation_id"])
                observation = LoopObservationEnvelope.model_validate(row.envelope)
            messages = await WorkspaceAttachments().project(sessions, observation, selected, [HumanMessage(content=instruction)])
            async with httpx.AsyncClient(timeout=45, event_hooks={"request": [inspect_request]}) as client:
                model = create_chat_model(name=selected.name, app_config=config,
                    http_async_client=client, max_retries=0, max_tokens=256)
                patrol = StructuredPatrolDecisionModel(config, selected.name)
                answer = await asyncio.wait_for(patrol._invoke(model, messages, "prompt_json", VisualAnswer), 60)
            assert answer.left_color == "red" and answer.right_shape == "circle"
            assert evidence and evidence[0]["image_sha256"] == material.digest
            print(json.dumps({"model": selected.name, "input_id": receipt["intent_id"],
                "material_id": material.material_id, "source_sha256": material.digest,
                "actual_http_images": evidence, "answer": answer.model_dump(), "model_calls": patrol.call_count}, ensure_ascii=False))
        finally:
            await engine.dispose()
    asyncio.run(run())
