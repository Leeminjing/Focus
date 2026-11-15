"""本文件对外提供图片附件交付、上下文保护与二进制读取兜底的回归测试。

输入为图片材料、运行上下文、模型请求和压缩决策；输出为请求层图片投影、
路径边界、送模缩放、材料失效诊断和压缩保护断言。具体工作流为不经桌面上传服务；上传资源、
独占命名和补偿协议由 test_material_upload_lifecycle.py 覆盖。

示例：python -m pytest backend/tests/test_image_attachment.py。
"""

import asyncio
import base64
import io
import json
import shutil
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.app.desktop.materials.files import (
    MATERIAL_ATTACHMENTS_SUBDIR,
    resolve_material_path,
)
from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.image_attachment import (
    ImageAttachmentProjectionMiddleware,
    ImageMaterialUnavailable,
    ImageModelCannotReadImages,
)
from focus.agents.image_inputs import MODEL_IMAGE_INPUT_KEY, RUN_IMAGE_INPUTS_CONTEXT_KEY
from focus.images import (
    IMAGE_MODEL_MAX_EDGE_PX,
    measure_image_tokens,
    scale_for_model,
)
from focus.messages import format_material_ref


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def scratch():
    """工作区内的临时目录。

    宿主沙箱下 %TEMP% 与 pytest 自带的 tmp_path 都建得成、清不掉（清理被 ACL 拒绝），
    只有工作区内自建目录可正常创建与删除，故在此自建；沙箱外可换回 tmp_path。
    """
    root = Path(__file__).resolve().parents[2] / "tmp" / f"image-attachment-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _FakeRuntime:
    def __init__(
        self, workspace: Path, materials: list[dict], image_capable: bool = True
    ) -> None:
        self.context = {
            "workspace": str(workspace),
            RUN_IMAGE_INPUTS_CONTEXT_KEY: {"attached": materials},
            MODEL_IMAGE_INPUT_KEY: image_capable,
        }


class _FakeRequest:
    def __init__(self, messages: list, runtime: _FakeRuntime) -> None:
        self.messages = messages
        self.runtime = runtime

    def override(self, **kwargs):
        clone = _FakeRequest(kwargs.get("messages", self.messages), self.runtime)
        return clone


class _CapturingHandler:
    def __init__(self) -> None:
        self.seen: list = []

    async def __call__(self, request):
        self.seen.append(request.messages)
        return "ok"


def test_attachments_subdir_is_hidden_and_nested():
    parts = MATERIAL_ATTACHMENTS_SUBDIR.split("/")
    assert MATERIAL_ATTACHMENTS_SUBDIR.startswith(".")
    assert len(parts) == 2
    assert parts[0].startswith(".")


def test_resolve_material_path_is_workspace_relative(scratch):
    relative = f"{MATERIAL_ATTACHMENTS_SUBDIR}/shot.png"
    assert resolve_material_path(scratch, relative) == scratch.resolve() / ".focus" / "attachments" / "shot.png"


def test_resolve_material_path_rejects_workspace_escape(scratch):
    with pytest.raises(ValueError, match="超出工作区"):
        resolve_material_path(scratch, "../escape.png")


def test_scale_for_model_keeps_small_image_untouched():
    original = _png(64, 64)
    mime, scaled = scale_for_model(original)
    assert mime == "image/png"
    assert scaled == original


def test_scale_for_model_shrinks_oversized_dimensions():
    from PIL import Image

    mime, scaled = scale_for_model(_png(3000, 1500))
    assert mime == "image/png"
    with Image.open(io.BytesIO(scaled)) as image:
        assert max(image.size) <= IMAGE_MODEL_MAX_EDGE_PX
    assert max(Image.open(io.BytesIO(scaled)).size) < 3000


def test_unparsable_bytes_are_passed_through_not_raised():
    mime, scaled = scale_for_model(b"not-an-image")
    assert scaled == b"not-an-image"


# === 4.2 中间件按 run 上下文注入像素 ===


def _middleware_request(scratch, materials):
    workspace = scratch
    return _FakeRequest([HumanMessage(content="看这张")], _FakeRuntime(workspace, materials))


def _run(coro):
    return asyncio.run(coro)


def test_middleware_injects_image_block(scratch):
    (scratch / "shot.png").write_bytes(_png(64, 64))
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    injected = handler.seen[0][-1]
    assert injected.content[1]["type"] == "image_url"
    assert injected.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_optional_attachment_is_projected_without_entering_state_or_completion_gate(scratch):
    data = _png(32, 32)
    (scratch / "shot.png").write_bytes(data)
    context = {
        "workspace": str(scratch),
        "model_supports_image_input": True,
        "run_image_inputs": {
            "attached": [{
                "material_id": "m1",
                "relative_path": "shot.png",
                "source_bytes": len(data),
                "model_bytes": len(data),
                "model_tokens": 1,
            }],
        },
    }
    request = _FakeRequest([HumanMessage(content="普通附件")], SimpleNamespace(context=context))
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    assert len(request.messages) == 1
    assert handler.seen[0][-1].content[1]["material_id"] == "m1"


def test_middleware_is_noop_without_materials(scratch):
    request = _middleware_request(scratch, [])
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    assert handler.seen[0] == request.messages


def test_middleware_injects_for_every_request(scratch):
    (scratch / "shot.png").write_bytes(_png(64, 64))
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    handler = _CapturingHandler()
    middleware = ImageAttachmentProjectionMiddleware()
    _run(middleware.awrap_model_call(request, handler))
    _run(middleware.awrap_model_call(request, handler))
    assert len(handler.seen) == 2
    for messages in handler.seen:
        assert messages[-1].content[1]["type"] == "image_url"


def test_injection_survives_reference_being_compressed_away(scratch):
    """清单来自 run 上下文而非消息：引用被摘要掉后依然注入。"""
    (scratch / "shot.png").write_bytes(_png(64, 64))
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="（这段已被压缩摘要替代）")], runtime)
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    assert handler.seen[0][-1].content[1]["type"] == "image_url"


def test_missing_material_fails_diagnosably(scratch):
    runtime = _FakeRuntime(scratch, [{"material_id": "m7", "relative_path": "gone.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(ImageMaterialUnavailable) as info:
        _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, _CapturingHandler()))
    assert "m7" in str(info.value)
    assert "gone.png" in str(info.value)


def test_material_path_cannot_escape_workspace(scratch):
    runtime = _FakeRuntime(scratch, [{"material_id": "m7", "relative_path": "../outside.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(ImageMaterialUnavailable, match="超出工作区"):
        _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, _CapturingHandler()))


def test_empty_material_fails_diagnosably(scratch):
    (scratch / "shot.png").write_bytes(b"")
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(ImageMaterialUnavailable):
        _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, _CapturingHandler()))


def test_model_without_image_capability_is_rejected(scratch):
    (scratch / "shot.png").write_bytes(_png(8, 8))
    runtime = _FakeRuntime(
        scratch,
        [{"material_id": "m1", "relative_path": "shot.png"}],
        image_capable=False,
    )
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(ImageModelCannotReadImages) as info:
        _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, _CapturingHandler()))
    assert "未声明具备图像输入能力" in str(info.value)


def test_no_materials_skips_capability_check(scratch):
    request = _FakeRequest(
        [HumanMessage(content="普通一轮")],
        _FakeRuntime(scratch, [], image_capable=False),
    )
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    assert handler.seen[0] == request.messages


# === 4.4 压缩豁免 ===


def _decision(source_ids):
    return {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": source_ids, "replacement": "摘要"}],
    }


def test_compression_without_protection_behaves_as_before():
    messages = [HumanMessage(content=f"看这张{format_material_ref(1, 'm1')}", id="h1")]
    ranges, error = validate_apply_decision(_decision(["h1"]), messages)
    assert error is None
    assert ranges[0]["source_ids"] == ["h1"]


# === 6.2 read_file 的二进制兜底 ===


class _ToolRuntime:
    def __init__(self, workspace: Path) -> None:
        from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
        from focus.security.policy import AccessMode

        self.context = SecurityContext(
            AuthorizationIdentity(workspace, (workspace,), ("read",), AccessMode.READ_ONLY, "main"),
            RoutingIdentity("images", "images", "main", "images", "", "images"),
        ).to_runtime_context()


def test_read_file_rejects_image_with_correctable_hint(scratch):
    (scratch / "shot.png").write_bytes(_png(8, 8))
    from focus.tools.builtins.workspace_tools import read_file

    with pytest.raises(Exception) as info:
        read_file.func("shot.png", _ToolRuntime(scratch))
    detail = str(info.value)
    assert "shot.png" in detail
    assert "图片材料" in detail
    assert "必须看" not in detail


def test_read_file_rejects_binary_with_correctable_hint(scratch):
    (scratch / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    from focus.tools.builtins.workspace_tools import read_file

    with pytest.raises(Exception) as info:
        read_file.func("blob.bin", _ToolRuntime(scratch))
    assert "二进制" in str(info.value)


def test_read_file_still_reads_text(scratch):
    (scratch / "notes.md").write_text("你好", encoding="utf-8")
    from focus.tools.builtins.workspace_tools import read_file

    assert json.loads(read_file.func("notes.md", _ToolRuntime(scratch)))["lines"][0]["text"] == "你好"


# === 随本变更确定的图像计量边界 ===


def test_measure_image_tokens_floor_is_one():
    assert measure_image_tokens(1, 1) >= 1


def test_data_url_round_trip():
    from focus.messages import image_inline_payload

    payload = base64.b64encode(_png(8, 8)).decode()
    block = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}}
    mime, data = image_inline_payload(block)
    assert mime == "image/png"
    assert data == payload


# === W4 图级别：必需材料不可读时运行不得成功 ===


def _fake_model(calls: list):
    import itertools

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class RecordingModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return RecordingModel(messages=itertools.cycle([AIMessage(content="回答")]))


def _run_graph(agent, messages, context, thread_id):
    async def run():
        async for _mode, _chunk in agent.astream(
            {"messages": messages},
            config={"configurable": {"thread_id": thread_id}},
            context=context,
            stream_mode=["values"],
        ):
            pass

    asyncio.run(run())


def test_agent_run_does_not_succeed_when_material_unreadable(scratch):
    from langchain.agents import create_agent

    workspace = scratch / "ws"
    workspace.mkdir()
    calls: list = []
    agent = create_agent(
        model=_fake_model(calls),
        tools=[],
        middleware=[ImageAttachmentProjectionMiddleware()],
        system_prompt="image attachment",
    )
    context = {
        "workspace": str(workspace),
        RUN_IMAGE_INPUTS_CONTEXT_KEY: {"attached": [{"material_id": "m1", "relative_path": "gone.png"}]},
        MODEL_IMAGE_INPUT_KEY: True,
    }
    with pytest.raises(ImageMaterialUnavailable):
        _run_graph(agent, [HumanMessage(content="看这张")], context, "image-attachment-missing")
    assert not calls, "图片附件不可读时模型不应被调用"


def test_agent_with_image_finishes_without_declaration(scratch):
    from langchain.agents import create_agent

    (scratch / "shot.png").write_bytes(_png(8, 8))
    calls = []
    agent = create_agent(model=_fake_model(calls), tools=[], middleware=[ImageAttachmentProjectionMiddleware()])
    context = {
        "workspace": str(scratch), MODEL_IMAGE_INPUT_KEY: True,
        RUN_IMAGE_INPUTS_CONTEXT_KEY: {
            "attached": [{"material_id": "m1", "relative_path": "shot.png"}],
            "required_ids": ["m1"],
        },
    }
    _run_graph(agent, [HumanMessage(content="核对图中尺寸")], context, "image-without-declaration")
    assert len(calls) == 1
    assert any(isinstance(message.content, list) and any(
        isinstance(block, dict) and block.get("type") == "image_url" for block in message.content
    ) for message in calls[0])


# === W5 图片累积触发压缩门 ===


def _gate_agent(calls: list):
    from langchain.agents import create_agent

    from focus.agents.compression.gate import build_compression_gate

    return create_agent(
        model=_fake_model(calls),
        tools=[],
        middleware=[build_compression_gate(context_window=400, threshold_ratio=0.5)],
        system_prompt="gate",
    )


def test_compression_gate_triggers_on_image_usage():
    """同一道门、同一个窗口，只有「含图」这一处差异决定是否被拦下。"""
    image_calls: list = []
    payload = base64.b64encode(_png(512, 512)).decode()
    image_message = {
        "role": "human",
        "content": [
            {"type": "text", "text": "看这张"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
        ],
    }
    try:
        _run_graph(_gate_agent(image_calls), [image_message], {}, "gate-image")
    except Exception:
        pass
    assert not image_calls, "含图上下文越过阈值，压缩门应在模型调用前拦下"

    text_calls: list = []
    _run_graph(_gate_agent(text_calls), [HumanMessage(content="看这张")], {}, "gate-text")
    assert text_calls, "同样窗口下的纯文本短消息不应触发压缩门"


# === D17 dsh-eyes 残骸已清除 ===


def test_lead_agent_state_has_no_legacy_image_residue():
    from focus.agents import lead_agent_state

    assert not hasattr(lead_agent_state, "ViewedImageData")
    assert not hasattr(lead_agent_state, "merge_viewed_images")
    assert "viewed_images" not in lead_agent_state.LeadAgentState.__annotations__
