"""必需图片交付、压缩豁免与二进制读取兜底的回归测试。

输入为图片材料、运行上下文、模型请求和压缩决策；输出为请求层图片投影、必需材料完成门、
路径边界、送模缩放、材料失效诊断和压缩保护断言。具体工作流不经桌面上传服务；上传资源、
独占命名和补偿协议由 test_material_upload_lifecycle.py 覆盖。

示例：python -m pytest backend/tests/test_must_view_images.py。
"""

import asyncio
import base64
import io
import shutil
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.app.desktop.material_files import (
    MATERIAL_ATTACHMENTS_SUBDIR,
    resolve_material_path,
)
from config_helpers import app_config_for as _app_config
from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.image_attachment import (
    ImageAttachmentProjectionMiddleware,
    ImageMaterialUnavailable,
    ImageModelCannotReadImages,
)
from focus.agents.must_view import (
    MUST_VIEW_CONTEXT_KEY,
    MODEL_IMAGE_INPUT_KEY,
    MustViewCompletionMiddleware,
)
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
    root = Path(__file__).resolve().parents[2] / "tmp" / f"must-view-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _FakeRuntime:
    def __init__(
        self, workspace: Path, materials: list[dict], image_capable: bool = True
    ) -> None:
        self.context = {
            "workspace": str(workspace),
            MUST_VIEW_CONTEXT_KEY: materials,
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
            "required_ids": [],
        },
    }
    request = _FakeRequest([HumanMessage(content="普通附件")], SimpleNamespace(context=context))
    handler = _CapturingHandler()
    _run(ImageAttachmentProjectionMiddleware().awrap_model_call(request, handler))
    assert len(request.messages) == 1
    assert handler.seen[0][-1].content[1]["material_id"] == "m1"
    assert MustViewCompletionMiddleware().after_model(
        {"messages": request.messages}, SimpleNamespace(context=context)
    ) is None


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
        middleware=[ImageAttachmentProjectionMiddleware(), MustViewCompletionMiddleware()],
        system_prompt="must-view",
    )
    context = {
        "workspace": str(workspace),
        MUST_VIEW_CONTEXT_KEY: [{"material_id": "m1", "relative_path": "gone.png"}],
        MODEL_IMAGE_INPUT_KEY: True,
    }
    with pytest.raises(ImageMaterialUnavailable):
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


# === N2 材料清单按类型分流 ===


def test_material_policy_line_marks_images_without_text_policies():
    from backend.app.desktop.service import _material_policy_line

    line = _material_policy_line(Path("/w/.focus/attachments/a.png"), "full", "strict")
    assert "图片材料" in line
    assert "优先完整阅读" not in line
    assert "严格遵守" not in line


def test_material_policy_line_keeps_text_policies_unchanged():
    from backend.app.desktop.service import _material_policy_line

    assert _material_policy_line(Path("/w/notes.md"), "full", "strict").endswith(
        "| 优先完整阅读 | 严格遵守"
    )
    assert _material_policy_line(Path("/w/notes.md"), "rough", "reference").endswith(
        "| 优先粗略阅读，需要时仍可完整读取 | 仅供参考"
    )


# === D17 dsh-eyes 残骸已清除 ===


def test_lead_agent_state_has_no_legacy_image_residue():
    from focus.agents import lead_agent_state

    assert not hasattr(lead_agent_state, "ViewedImageData")
    assert not hasattr(lead_agent_state, "merge_viewed_images")
    assert "viewed_images" not in lead_agent_state.LeadAgentState.__annotations__


# === 组 9 逐图表态（报告工具） ===


class _Runtime:
    def __init__(self, context: dict) -> None:
        self.context = context


def _report_message(reports: dict[str, bool]) -> AIMessage:
    return AIMessage(
        content="已逐张查看。",
        tool_calls=[{
            "name": "report_must_view_images",
            "args": {
                "images": [
                    {"material_id": key, "read": value} for key, value in reports.items()
                ]
            },
            "id": "report-must-view",
            "type": "tool_call",
        }],
    )


def _reports_state(context: dict, reports: dict[str, bool] | None, reminders: int = 0) -> dict:
    messages = [HumanMessage(content="看这张")]
    messages += [
        HumanMessage(content=f"[focus-must-view] 催促 {index}") for index in range(reminders)
    ]
    if reports is not None:
        messages.append(_report_message(reports))
    return {"messages": messages}


TWO_IMAGES = [
    {"material_id": "m1", "relative_path": "a.png"},
    {"material_id": "m2", "relative_path": "b.png"},
]


def test_reports_schema_carries_material_and_read_flag():
    from focus.agents.must_view import MustViewImageReport, MustViewReports

    reports = MustViewReports(images=[MustViewImageReport(material_id="m1", read=True)])
    assert reports.images[0].material_id == "m1"
    assert reports.images[0].read is True


def test_must_view_prompt_lists_materials_and_names_report_tool():
    from backend.app.desktop.service import _must_view_prompt

    text = _must_view_prompt([{"material_id": "m1", "relative_path": "a.png"}])
    assert "a.png" in text
    assert "material_id=m1" in text
    assert "read" in text
    assert "report_must_view_images" in text


def test_report_tool_advertises_material_and_read_fields():
    """表态工具的 schema 就是提示与门之间的契约：字段名必须一致。"""
    from focus.agents.must_view import MustViewImageReport, report_must_view_images

    assert report_must_view_images.name == "report_must_view_images"
    field = report_must_view_images.args_schema.model_fields["images"]
    assert field.annotation == list[MustViewImageReport]
    assert set(MustViewImageReport.model_fields) == {"material_id", "read"}
    assert "read" in report_must_view_images.description


def test_report_tool_binds_without_forcing_tool_choice():
    """根因回归：表态走普通工具，绑定后的请求载荷不得出现被强制的 tool_choice。

    改回 provider 结构化输出（ToolStrategy）时 LangChain 会把 tool_choice 置为 required，
    与 thinking 模型互斥（400 Thinking mode does not support this tool_choice）。
    """
    from focus.agents.must_view import report_must_view_images
    from focus.models.deepseek import DeepSeekChatOpenAI

    model = DeepSeekChatOpenAI(
        model="deepseek-v4-flash-vision-exp",
        api_key="test-key",
        base_url="https://api.deepseek.com",
    )
    bound = model.bind_tools([report_must_view_images])
    assert "tool_choice" not in bound.kwargs, "表态工具不得给本轮模型调用带来强制工具选择"
    tools = bound.kwargs["tools"]
    assert tools[0]["function"]["name"] == "report_must_view_images"
    assert "material_id" in str(tools[0]["function"]["parameters"])


def test_after_model_reads_report_alongside_other_tool_calls():
    """模型同一轮里既查文件又表态时，表态必须被采纳。"""
    context = {"must_view_materials": TWO_IMAGES}
    message = _report_message({"m1": True, "m2": True})
    message.tool_calls.append({"name": "read_file", "args": {"path": "a.md"}, "id": "call-1"})
    state = {"messages": [HumanMessage(content="看这张"), message]}
    assert MustViewCompletionMiddleware().after_model(state, _Runtime(context)) is None


def test_after_model_ignores_other_tool_calls():
    context = {"must_view_materials": TWO_IMAGES}
    message = AIMessage(
        content="先读文件",
        tool_calls=[{"name": "read_file", "args": {"path": "a.md"}, "id": "call-1"}],
    )
    state = {"messages": [HumanMessage(content="看这张"), message]}
    result = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert result["jump_to"] == "model"
    assert any("a.png" in str(item.content) for item in result["messages"])


def test_after_model_ignores_malformed_report_args():
    """工具参数不合逐图声明结构时按「未表态」处理，MUST NOT 把整轮判为失败。"""
    context = {"must_view_materials": TWO_IMAGES}
    message = AIMessage(
        content="表态",
        tool_calls=[{
            "name": "report_must_view_images",
            "args": {"images": [{"material_id": "m1"}]},
            "id": "call-1",
        }],
    )
    state = {"messages": [HumanMessage(content="看这张"), message]}
    result = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert result["jump_to"] == "model"


def test_after_model_keeps_declaration_from_earlier_message():
    """模型先给出完整表态、后续轮次不再重复时，门仍应放行。"""
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True, "m2": True})
    state["messages"].append(AIMessage(content="继续干活"))
    assert MustViewCompletionMiddleware().after_model(state, _Runtime(context)) is None


def test_after_model_does_not_read_its_own_reminder_as_declaration():
    """催促语里带着工具名；它属于 HumanMessage，绝不能被算作本轮表态。"""
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, None)
    reminder = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert reminder["jump_to"] == "model"
    state = {"messages": [*state["messages"], *reminder["messages"]]}
    again = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert again["jump_to"] == "model", "催促语里的模板不得被当作模型表态"


def test_after_model_passes_once_every_material_is_reported():
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True, "m2": True})
    assert MustViewCompletionMiddleware().after_model(state, _Runtime(context)) is None


def test_after_model_only_requires_required_subset():
    context = {
        "run_image_inputs": {
            "attached": [*TWO_IMAGES, {"material_id": "m3", "relative_path": "optional.png"}],
            "required_ids": ["m2"],
        }
    }
    state = _reports_state(context, {"m2": True})
    assert MustViewCompletionMiddleware().after_model(state, _Runtime(context)) is None


def test_after_model_refuses_to_end_with_missing_report():
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True})
    result = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert result["jump_to"] == "model"
    assert any("b.png" in str(message.content) for message in result["messages"])


def test_after_model_ignores_runs_without_must_view():
    context = {"must_view_materials": []}
    state = _reports_state(context, None)
    assert MustViewCompletionMiddleware().after_model(state, _Runtime(context)) is None


def test_after_model_escalates_when_model_reports_unread(monkeypatch):
    import focus.agents.must_view as must_view

    seen: dict = {}
    monkeypatch.setattr(must_view, "interrupt", lambda payload: seen.update(payload))
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True, "m2": False})
    MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert seen["type"] == "must_view_report"
    assert seen["unread"] == ["m2"]


def test_after_model_escalates_after_reminder_limit(monkeypatch):
    import focus.agents.must_view as must_view

    seen: dict = {}
    monkeypatch.setattr(must_view, "interrupt", lambda payload: seen.update(payload))
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True}, reminders=must_view._MAX_REMINDERS)
    MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert seen["missing"] == ["m2"]


@pytest.mark.parametrize(
    ("decision", "jump_to"),
    [("retry", "model"), ("cancel", "end")],
)
def test_after_model_handles_report_recovery_decisions(monkeypatch, decision, jump_to):
    import focus.agents.must_view as must_view

    monkeypatch.setattr(
        must_view,
        "interrupt",
        lambda _payload: {"type": "must_view_report", "decision": decision},
    )
    context = {"must_view_materials": TWO_IMAGES}
    state = _reports_state(context, {"m1": True, "m2": False})
    result = MustViewCompletionMiddleware().after_model(state, _Runtime(context))
    assert result["jump_to"] == jump_to
    assert bool(result.get("messages")) is (decision == "retry")
