"""dsh-eyes 视觉插件测试:剥离 hook、附件索引、view_image 校验、端点协议、装配集成。"""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from focus.plugins.interfaces import builtin_catalog
from focus.plugins.registry import PluginRegistry

PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "dsh-eyes"


def _fake_config(**overrides):
    config = {
        "vision_model": "qwen3.7-plus",
        "vision_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "vision_api_key": "sk-test",
        "max_image_bytes": 15728640,
        "passthrough_models": [],
    }
    config.update(overrides)
    return config


class _FakeRuntime:
    def __init__(self, thread_id="th-1", model_name=None):
        self.execution_info = type("Info", (), {"thread_id": thread_id, "model_name": model_name})()


# === 剥离 hook ===


@pytest.fixture
def isolated_index(tmp_path, monkeypatch):
    """隔离附件索引到 tmp:重置单例并指向 tmp 数据目录。"""
    import plugins.dsh_eyes.index as index_module

    monkeypatch.setattr(index_module, "_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(index_module, "_ATTACH_DIR", tmp_path / "data" / "attachments")
    monkeypatch.setattr(index_module, "_INDEX_FILE", tmp_path / "data" / "index.json")
    monkeypatch.setattr(index_module, "_index", None)
    yield index_module
    monkeypatch.setattr(index_module, "_index", None)


def _image_message():
    return HumanMessage(
        content=[
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )


def test_strip_replaces_image_block_with_reference(isolated_index, monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        diff = await strip_images(
            {"messages": [_image_message()]}, _FakeRuntime(),
        )
        content = diff["messages"][0].content
        assert content[0] == {"type": "text", "text": "看这张图"}
        assert content[1]["type"] == "text"
        assert "【图片1 attachment_id=" in content[1]["text"]

    asyncio.run(run())


def test_strip_plain_text_returns_none(isolated_index, monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        diff = await strip_images(
            {"messages": [HumanMessage(content="纯文本")]}, _FakeRuntime(),
        )
        assert diff is None

    asyncio.run(run())


def test_strip_passthrough_when_model_in_list(isolated_index, monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config",
        lambda: _fake_config(passthrough_models=["qwen3.7-plus"]),
    )
    monkeypatch.setattr("plugins.dsh_eyes.strip._main_model_name", "qwen3.7-plus")

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        diff = await strip_images(
            {"messages": [_image_message()]}, _FakeRuntime(),
        )
        assert diff is None

    asyncio.run(run())


def test_strip_passthrough_when_system_model_multimodal(isolated_index, monkeypatch):
    """系统主模型非 DeepSeek 前缀 → 视为原生多模态,不剥离。"""
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )
    monkeypatch.setattr("plugins.dsh_eyes.strip._main_model_name", "gpt-4o")

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        diff = await strip_images(
            {"messages": [_image_message()]}, _FakeRuntime(),
        )
        assert diff is None

    asyncio.run(run())


def test_strip_reference_contains_view_hint(isolated_index, monkeypatch):
    """剥离文本含调用提示(对齐 dsh-eyes)。"""
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )
    monkeypatch.setattr("plugins.dsh_eyes.strip._main_model_name", "deepseek-v4-flash")

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        diff = await strip_images(
            {"messages": [_image_message()]}, _FakeRuntime(),
        )
        text = diff["messages"][0].content[1]["text"]
        assert "查看请调 view_image(attachment_id=" in text

    asyncio.run(run())


def test_strip_invalid_url_notes_unlocatable(isolated_index, monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )
    monkeypatch.setattr("plugins.dsh_eyes.strip._main_model_name", "deepseek-v4-flash")

    async def run():
        from plugins.dsh_eyes.strip import strip_images

        message = HumanMessage(content=[
            {"type": "image_url", "image_url": {}},
        ])
        diff = await strip_images({"messages": [message]}, _FakeRuntime())
        text = diff["messages"][0].content[0]["text"]
        assert "无法定位其附件 id" in text

    asyncio.run(run())


def test_strip_same_url_reuses_attachment_id(isolated_index, monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )

    async def run():
        from plugins.dsh_eyes.index import get_index
        from plugins.dsh_eyes.strip import strip_images

        await strip_images({"messages": [_image_message()]}, _FakeRuntime())
        await strip_images({"messages": [_image_message()]}, _FakeRuntime())
        bucket = get_index()._data["th-1"]
        assert len(bucket) == 1  # 同 url 命中既有 id,不重复登记

    asyncio.run(run())


# === 附件索引 ===


def test_index_session_isolation(isolated_index):
    from plugins.dsh_eyes.index import get_index

    async def run():
        index = get_index()
        await index.attach_bytes("th-a", "att-1", "data:image/png;base64,WA==", 1024)
        entry = index.get("th-a", "att-1")
        assert entry["file"] == "attachments/att-1.png"
        assert index.get("th-b", "att-1") is None  # 会话隔离

    asyncio.run(run())


def test_index_bytes_persist_and_resolve(isolated_index):
    from plugins.dsh_eyes.index import get_index

    async def run():
        index = get_index()
        await index.attach_bytes("th-a", "att-1", "data:image/png;base64,WA==", 1024)
        assert index.resolve_data_url("th-a", "att-1") == "data:image/png;base64,WA=="

    asyncio.run(run())
    assert (Path(isolated_index._DATA_DIR) / "index.json").is_file()
    assert (Path(isolated_index._DATA_DIR) / "attachments" / "att-1.png").is_file()


def test_index_attach_is_idempotent(isolated_index):
    from plugins.dsh_eyes.index import get_index

    async def run():
        index = get_index()
        await index.attach_bytes("th-a", "att-1", "data:image/png;base64,WA==", 1024)
        await index.attach_bytes("th-a", "att-1", "data:image/png;base64,WA==", 1024)
        assert len(index._data["th-a"]) == 1
        files = list((Path(isolated_index._DATA_DIR) / "attachments").iterdir())
        assert len(files) == 1  # 幂等:不重复写文件

    asyncio.run(run())


def test_index_legacy_url_entry_compatible(isolated_index):
    """旧格式(条目直接含 url)仍可解析(升级兼容)。"""
    from plugins.dsh_eyes.index import get_index

    async def run():
        index = get_index()
        index._data["th-a"] = {"att-old": {"url": "data:image/png;base64,AA=="}}
        assert index.resolve_data_url("th-a", "att-old") == "data:image/png;base64,AA=="

    asyncio.run(run())


def test_index_attach_rejects_oversize(isolated_index):
    from plugins.dsh_eyes.index import get_index

    async def run():
        index = get_index()
        with pytest.raises(ValueError, match="超过上限"):
            await index.attach_bytes("th-a", "att-1", "data:image/png;base64,WA==", 0)

    asyncio.run(run())


# === view_image 行为(对齐 dsh-eyes) ===


def test_view_image_empty_input_asks_for_input():
    async def run():
        from plugins.dsh_eyes.tool import view_image

        text = await view_image.ainvoke({})
        assert text == "view_image 错误:请提供 attachment_ids、attachment_id 或 image_path。"

    asyncio.run(run())


def test_view_image_attachment_session_isolation(isolated_index):
    async def run():
        from plugins.dsh_eyes.tool import view_image

        text = await view_image.ainvoke({"attachment_id": "not-in-session"})
        assert "找不到 attachment_id=not-in-session 对应的图片" in text

    asyncio.run(run())


def test_view_image_attachment_ids_take_priority(isolated_index, monkeypatch):
    """多输入非互斥:attachment_ids 优先(对齐 dsh-eyes)。"""
    async def fake_call(config, data_url, prompt=None):
        return "FAKE-VISION-RESULT"

    monkeypatch.setattr("plugins.dsh_eyes.eyes.call_vision", fake_call)

    async def run():
        from plugins.dsh_eyes.index import get_index
        from plugins.dsh_eyes.tool import view_image

        index = get_index()
        await index.attach_bytes("unknown", "att-1", "data:image/png;base64,WA==", 1024)
        text = await view_image.ainvoke({
            "attachment_ids": ["att-1"],
            "image_path": "ignored.png",
        })
        assert "【图片1】FAKE-VISION-RESULT" in text

    asyncio.run(run())


def test_view_image_single_and_batch_labels(isolated_index, monkeypatch):
    """结果格式:单图【图片】、多图【图片N】(对齐 dsh-eyes)。"""
    async def fake_call(config, data_url, prompt=None):
        return "FAKE-VISION-RESULT"

    monkeypatch.setattr("plugins.dsh_eyes.eyes.call_vision", fake_call)

    async def run():
        from plugins.dsh_eyes.index import get_index
        from plugins.dsh_eyes.tool import view_image

        index = get_index()
        for aid in ("att-1", "att-2"):
            await index.attach_bytes("unknown", aid, "data:image/png;base64,WA==", 1024)
        single = await view_image.ainvoke({"attachment_id": "att-1"})
        assert single == "【图片】FAKE-VISION-RESULT"
        batch = await view_image.ainvoke({"attachment_ids": ["att-1", "att-2"]})
        assert "【图片1】FAKE-VISION-RESULT" in batch
        assert "【图片2】FAKE-VISION-RESULT" in batch

    asyncio.run(run())


def test_view_image_batch_missing_id_error_item(isolated_index, monkeypatch):
    """多图中缺失 id → 【图片N】错误条目,其余照常(对齐 dsh-eyes)。"""
    async def fake_call(config, data_url, prompt=None):
        return "OK"

    monkeypatch.setattr("plugins.dsh_eyes.eyes.call_vision", fake_call)

    async def run():
        from plugins.dsh_eyes.index import get_index
        from plugins.dsh_eyes.tool import view_image

        index = get_index()
        await index.attach_bytes("unknown", "att-1", "data:image/png;base64,WA==", 1024)
        text = await view_image.ainvoke({"attachment_ids": ["att-1", "missing"]})
        assert "【图片1】OK" in text
        assert "【图片2】错误:找不到 attachment_id=missing" in text

    asyncio.run(run())


def test_view_image_path_outside_workspace(monkeypatch):
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )

    async def run():
        from plugins.dsh_eyes.tool import _read_workspace_image

        data_url, error = _read_workspace_image(r"D:\outside.png", r"C:\ws", 15728640)
        assert data_url is None
        assert "无法定位文件" in error

    asyncio.run(run())


def test_view_image_size_limit(tmp_path):
    (tmp_path / "big.png").write_bytes(b"12345")

    async def run():
        from plugins.dsh_eyes.tool import _read_workspace_image

        data_url, error = _read_workspace_image("big.png", str(tmp_path), 4)
        assert data_url is None
        assert "超过上限" in error

    asyncio.run(run())


# === 端点协议与视觉请求 ===


def test_endpoint_auto_completion():
    from plugins.dsh_eyes.config import endpoint_url

    assert endpoint_url(_fake_config()) == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert endpoint_url(_fake_config(
        vision_endpoint="https://x.example/v1/chat/completions",
    )) == "https://x.example/v1/chat/completions"


def test_describe_image_passes_prompt(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "有猫"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["body"] = json
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        from plugins.dsh_eyes.eyes import describe_image

        text = await describe_image(_fake_config(), "data:image/png;base64,X", "识别图中所有文字")
        assert text == "有猫"
        blocks = captured["body"]["messages"][0]["content"]
        assert blocks[0]["text"] == "识别图中所有文字"

    asyncio.run(run())


def test_describe_image_retries_on_429_then_succeeds(monkeypatch):
    """429 重试后成功(对齐 dsh-eyes 瞬态重试)。"""
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            if calls["n"] == 2:
                return {"choices": [{"message": {"content": "重试后成功"}}]}
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return type("R", (), {"status_code": 429, "text": "rate limited"})()
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        from plugins.dsh_eyes.eyes import describe_image

        text = await describe_image(_fake_config(), "data:image/png;base64,X")
        assert text == "重试后成功"
        assert calls["n"] == 2

    asyncio.run(run())


def test_describe_image_400_fails_immediately(monkeypatch):
    """非 429 的 4xx 立即失败,不重试。"""
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            calls["n"] += 1
            return type("R", (), {"status_code": 400, "text": "bad request"})()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        from plugins.dsh_eyes.eyes import describe_image

        text = await describe_image(_fake_config(), "data:image/png;base64,X")
        assert "HTTP 400" in text
        assert calls["n"] == 1

    asyncio.run(run())


def test_responses_protocol_body_and_parsing(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "responses 解析结果"},
            ]}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        from plugins.dsh_eyes.eyes import describe_image

        config = _fake_config(vision_endpoint="https://x.example/v1/responses")
        text = await describe_image(config, "data:image/png;base64,X")
        assert text == "responses 解析结果"
        assert captured["url"] == "https://x.example/v1/responses"
        blocks = captured["body"]["input"][0]["content"]
        assert blocks[0]["type"] == "input_text"
        assert blocks[1]["type"] == "input_image"

    asyncio.run(run())


def test_describe_image_http_error_returns_text(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = "server exploded"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        from plugins.dsh_eyes.eyes import describe_image

        text = await describe_image(_fake_config(), "data:image/png;base64,X")
        assert "HTTP 500" in text

    asyncio.run(run())


# === 粘贴图片消息通道 ===


def test_main_run_create_accepts_content_blocks():
    from backend.app.desktop.models import MainRunCreate

    body = MainRunCreate(message=[
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    assert isinstance(body.message, list)
    assert body.message[0]["type"] == "text"
    # 纯文本形态保持兼容
    assert MainRunCreate(message="纯文本").message == "纯文本"
    # 空消息仍被拒绝
    with pytest.raises(Exception):
        MainRunCreate(message=[])


def test_deserialize_keeps_image_blocks():
    from focus.runtime.runs.events import deserialize_messages

    messages = deserialize_messages([{
        "role": "human",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }])
    assert len(messages) == 1
    content = messages[0].content
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_strip_works_on_deserialized_message(isolated_index, monkeypatch):
    """粘贴通道端到端:content 列表经 deserialize 还原后,剥离 hook 正常替换。"""
    from plugins.dsh_eyes.config import load_config

    monkeypatch.setattr(
        "plugins.dsh_eyes.config.load_config", lambda: _fake_config(),
    )

    async def run():
        from focus.runtime.runs.events import deserialize_messages

        from plugins.dsh_eyes.strip import strip_images

        messages = deserialize_messages([{
            "role": "human",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }])
        diff = await strip_images({"messages": messages}, _FakeRuntime())
        content = diff["messages"][0].content
        assert content[0] == {"type": "text", "text": "看这张图"}
        assert "【图片1 attachment_id=" in content[1]["text"]

    asyncio.run(run())


# === 装配集成 ===


def test_plugin_loads_active_with_tool_and_hook(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    shutil.copytree(PLUGIN_SOURCE, root / "dsh-eyes")
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["dsh-eyes"]["status"] == "active"
    assert [tool_.name for tool_ in registry.tools()] == ["view_image"]
    assert [impl.plugin for impl in registry.hooks("hook.before_model")] == ["dsh-eyes"]
