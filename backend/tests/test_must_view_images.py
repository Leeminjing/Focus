"""必需图片注入、压缩豁免与二进制读取兜底的回归测试。

覆盖:材料落盘到工作区专用目录且不覆盖同名、原图体积上限、必需材料清单的校验、
中间件在请求层注入像素（清单来自 run 上下文而非消息）、必需材料不可读时以可诊断失败收场、
压缩范围不得覆盖必需图片所依赖的消息、read_file 对图片与二进制内容给出可修正提示。
"""

import asyncio
import base64
import io
import shutil
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from backend.app.desktop.material_files import (
    MATERIAL_ATTACHMENTS_SUBDIR,
    EmptyUpload,
    OversizedImage,
    guard_upload,
    prepare_attachment_target,
    resolve_material_path,
)
from config_helpers import app_config_for as _app_config
from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.must_view import (
    MUST_VIEW_CONTEXT_KEY,
    MustViewImagesMiddleware,
    MustViewMaterialUnavailable,
)
from focus.images import (
    IMAGE_MODEL_MAX_EDGE_PX,
    ORIGINAL_IMAGE_MAX_BYTES,
    image_exceeds_original_limit,
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
    root = Path(__file__).resolve().parents[2] / "tmp" / f"must-view-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _FakeRuntime:
    def __init__(self, workspace: Path, materials: list[dict]) -> None:
        self.context = {"workspace": str(workspace), MUST_VIEW_CONTEXT_KEY: materials}


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


# === 2.2 落盘位置与唯一命名 ===


def test_attachment_target_creates_dir_and_never_overwrites(scratch):
    target = prepare_attachment_target(scratch, "shot.png")
    assert target.parent.name == "attachments"
    assert target.parent.is_dir()
    assert not target.exists()
    target.write_bytes(b"x")
    second = prepare_attachment_target(scratch, "shot.png")
    assert second != target
    assert second.name.startswith("shot-")
    assert second.suffix == ".png"
    assert target.read_bytes() == b"x"
    assert not second.exists()


def test_attachment_target_strips_directory_components(scratch):
    target = prepare_attachment_target(scratch, "../../escape.png")
    assert target.parent.name == "attachments"
    assert target.name == "escape.png"


def test_attachments_subdir_is_hidden_and_nested():
    parts = MATERIAL_ATTACHMENTS_SUBDIR.split("/")
    assert MATERIAL_ATTACHMENTS_SUBDIR.startswith(".")
    assert len(parts) == 2
    assert parts[0].startswith(".")


def test_resolve_material_path_is_workspace_relative(scratch):
    relative = f"{MATERIAL_ATTACHMENTS_SUBDIR}/shot.png"
    assert resolve_material_path(scratch, relative) == scratch.resolve() / ".focus" / "attachments" / "shot.png"


# === 2.3 原图体积上限与送模缩放分离 ===


def test_original_limit_only_applies_to_images():
    assert image_exceeds_original_limit("shot.png", ORIGINAL_IMAGE_MAX_BYTES + 1) is True
    assert image_exceeds_original_limit("shot.png", ORIGINAL_IMAGE_MAX_BYTES) is False
    assert image_exceeds_original_limit("notes.md", ORIGINAL_IMAGE_MAX_BYTES + 1) is False


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
    _run(MustViewImagesMiddleware().awrap_model_call(request, handler))
    injected = handler.seen[0][-1]
    assert injected.content[1]["type"] == "image_url"
    assert injected.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_middleware_is_noop_without_materials(scratch):
    request = _middleware_request(scratch, [])
    handler = _CapturingHandler()
    _run(MustViewImagesMiddleware().awrap_model_call(request, handler))
    assert handler.seen[0] == request.messages


def test_middleware_injects_for_every_request(scratch):
    (scratch / "shot.png").write_bytes(_png(64, 64))
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    handler = _CapturingHandler()
    middleware = MustViewImagesMiddleware()
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
    _run(MustViewImagesMiddleware().awrap_model_call(request, handler))
    assert handler.seen[0][-1].content[1]["type"] == "image_url"


def test_missing_material_fails_diagnosably(scratch):
    runtime = _FakeRuntime(scratch, [{"material_id": "m7", "relative_path": "gone.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(MustViewMaterialUnavailable) as info:
        _run(MustViewImagesMiddleware().awrap_model_call(request, _CapturingHandler()))
    assert "m7" in str(info.value)
    assert "gone.png" in str(info.value)


def test_empty_material_fails_diagnosably(scratch):
    (scratch / "shot.png").write_bytes(b"")
    runtime = _FakeRuntime(scratch, [{"material_id": "m1", "relative_path": "shot.png"}])
    request = _FakeRequest([HumanMessage(content="看这张")], runtime)
    with pytest.raises(MustViewMaterialUnavailable):
        _run(MustViewImagesMiddleware().awrap_model_call(request, _CapturingHandler()))


# === 4.4 压缩豁免 ===


def _decision(source_ids):
    return {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": source_ids, "replacement": "摘要"}],
    }


def test_compression_rejects_range_covering_must_view_reference():
    messages = [
        HumanMessage(content=f"看这张{format_material_ref(1, 'm1')}", id="h1"),
        HumanMessage(content="后续追问", id="h2"),
    ]
    ranges, error = validate_apply_decision(_decision(["h1"]), messages, ("m1",))
    assert ranges == []
    assert error and "必须查看" in error


def test_compression_allows_range_without_must_view_reference():
    messages = [
        HumanMessage(content=f"看这张{format_material_ref(1, 'm1')}", id="h1"),
        HumanMessage(content="后续追问", id="h2"),
    ]
    ranges, error = validate_apply_decision(_decision(["h2"]), messages, ("m1",))
    assert error is None
    assert ranges[0]["source_ids"] == ["h2"]


def test_compression_allows_previous_run_reference():
    """此前 run 的图片不在本轮清单里 → 不受保护，可被压缩。"""
    messages = [HumanMessage(content=f"旧图{format_material_ref(1, 'old')}", id="h1")]
    ranges, error = validate_apply_decision(_decision(["h1"]), messages, ("m1",))
    assert error is None
    assert ranges[0]["source_ids"] == ["h1"]


def test_compression_without_protection_behaves_as_before():
    messages = [HumanMessage(content=f"看这张{format_material_ref(1, 'm1')}", id="h1")]
    ranges, error = validate_apply_decision(_decision(["h1"]), messages)
    assert error is None
    assert ranges[0]["source_ids"] == ["h1"]


# === 6.2 read_file 的二进制兜底 ===


class _ToolRuntime:
    def __init__(self, workspace: Path) -> None:
        self.context = {"workspace": str(workspace), "permissions": ["read"]}


def test_read_file_rejects_image_with_correctable_hint(scratch):
    (scratch / "shot.png").write_bytes(_png(8, 8))
    from focus.tools.builtins.workspace_tools import read_file

    with pytest.raises(Exception) as info:
        read_file.func("shot.png", _ToolRuntime(scratch))
    detail = str(info.value)
    assert "shot.png" in detail
    assert "必须看" in detail


def test_read_file_rejects_binary_with_correctable_hint(scratch):
    (scratch / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    from focus.tools.builtins.workspace_tools import read_file

    with pytest.raises(Exception) as info:
        read_file.func("blob.bin", _ToolRuntime(scratch))
    assert "二进制" in str(info.value)


def test_read_file_still_reads_text(scratch):
    (scratch / "notes.md").write_text("你好", encoding="utf-8")
    from focus.tools.builtins.workspace_tools import read_file

    assert read_file.func("notes.md", _ToolRuntime(scratch)) == "你好"


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


# === S3 落盘前把关是纯判定，不产生任何文件 ===


def test_guard_upload_rejects_without_touching_the_filesystem(scratch):
    with pytest.raises(OversizedImage):
        guard_upload("shot.png", b"x" * (ORIGINAL_IMAGE_MAX_BYTES + 1))
    with pytest.raises(EmptyUpload):
        guard_upload("shot.png", b"")
    assert not (scratch / ".focus").exists(), "被拒绝的上传不得留下任何文件"
    guard_upload("notes.md", b"x" * (ORIGINAL_IMAGE_MAX_BYTES + 1))


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
        middleware=[MustViewImagesMiddleware()],
        system_prompt="must-view",
    )
    context = {
        "workspace": str(workspace),
        MUST_VIEW_CONTEXT_KEY: [{"material_id": "m1", "relative_path": "gone.png"}],
    }
    with pytest.raises(MustViewMaterialUnavailable):
        _run_graph(agent, [HumanMessage(content="看这张")], context, "must-view-missing")
    assert not calls, "必需图片不可读时模型不应被调用"


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


# === W6 依赖视觉的插件可用性随模型声明而变 ===


def test_spatial_plugin_unavailable_when_model_is_text_only(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    import plugins.spatial_patrol.spatial as spatial

    monkeypatch.setattr(
        spatial, "get_app_config", lambda _path: _app_config("deepseek-v4-flash", None)
    )
    with pytest.raises(RuntimeError, match="纯文本模型且无视觉插件"):
        spatial.init_service({"vision_model": ""}, registry=None)


def test_spatial_plugin_available_when_model_declares_vision(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    import plugins.spatial_patrol.spatial as spatial

    monkeypatch.setattr(
        spatial, "get_app_config", lambda _path: _app_config("some-vision-model", True)
    )
    assert spatial.init_service({"vision_model": ""}, registry=None) is not None
