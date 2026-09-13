"""图片上下文核算与视觉能力判定的回归测试。

覆盖:图片内容块不再计为零、纯文本口径逐位不变、编码体积不作为文本长度计入、
压缩触发判定与窗口校验共用同一图片口径、模型条目显式声明取代模型名前缀推断。
"""

import base64
import io

import pytest

from backend.app.desktop.service import estimate_tokens
from focus.config.app_config import AppConfig
from config_helpers import app_config_for as _app_config
from focus.messages import (
    estimate_image_tokens,
    estimate_images_tokens,
    estimate_messages_tokens,
    estimate_raw_tokens,
)

TEXT_MESSAGE = {"role": "human", "content": [{"type": "text", "text": "看这张"}]}


def _png_base64(width: int, height: int) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _image_message(width: int, height: int) -> dict:
    url = f"data:image/png;base64,{_png_base64(width, height)}"
    return {
        "role": "human",
        "content": [*TEXT_MESSAGE["content"], {"type": "image_url", "image_url": {"url": url}}],
    }


# === 1.3 图片不再计为零 ===


def test_image_block_contributes_tokens():
    messages = [_image_message(256, 256)]
    assert estimate_messages_tokens(messages) > estimate_messages_tokens([TEXT_MESSAGE])
    assert estimate_images_tokens(messages) > 0


def test_image_is_the_only_image_contribution():
    """图片消息与同文本消息的差值必须恰好等于图片口径的折算值。"""
    messages = [_image_message(512, 512)]
    delta = estimate_messages_tokens(messages) - estimate_messages_tokens([TEXT_MESSAGE])
    assert delta == estimate_images_tokens(messages)


def test_plain_text_estimate_is_unchanged():
    assert estimate_images_tokens([TEXT_MESSAGE]) == 0
    assert estimate_messages_tokens([TEXT_MESSAGE]) == estimate_raw_tokens("看这张", 1)
    messages = [
        {"role": "human", "content": "你好"},
        {"role": "ai", "content": "abcd"},
    ]
    assert estimate_messages_tokens(messages) == estimate_raw_tokens("你好\nabcd", 2)


# === 图片口径按尺寸折算 ===


def test_image_tokens_follow_dimensions():
    assert estimate_image_tokens(64, 64) == 4
    assert estimate_image_tokens(512, 512) == 256
    assert estimate_image_tokens(512, 512) > estimate_image_tokens(64, 64)


def test_image_tokens_cap_long_edge():
    assert estimate_image_tokens(4096, 4096) == estimate_image_tokens(2048, 2048)
    assert estimate_image_tokens(10240, 512) == estimate_image_tokens(2048, 102)


def test_unparsable_payload_is_not_zero():
    messages = [
        {
            "role": "human",
            "content": [{"type": "image", "base64": "bm90LWFuLWltYWdl", "mime_type": "image/png"}],
        }
    ]
    assert estimate_images_tokens(messages) > 0


# === 1.4 窗口校验与压缩触发同口径 ===


def test_window_estimate_does_not_count_base64_as_text():
    messages = [_image_message(512, 512)]
    url_length = len(messages[0]["content"][1]["image_url"]["url"])
    estimate = estimate_tokens("", messages, "")
    assert estimate < url_length // 4


def test_window_estimate_grows_with_image():
    messages = [_image_message(512, 512)]
    assert estimate_tokens("", messages, "") > estimate_tokens("", [TEXT_MESSAGE], "")


# === 1.2 视觉能力判定只依据显式声明 ===


def test_vision_named_model_without_declaration_is_text_only(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    from plugins.spatial_patrol.spatial import _main_model_text_only

    assert _main_model_text_only(_app_config("deepseek-v4-flash-vision-exp", None)) is True


def test_plain_named_model_with_declaration_is_not_text_only(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    from plugins.spatial_patrol.spatial import _main_model_text_only

    assert _main_model_text_only(_app_config("some-text-model", True)) is False


def test_missing_declaration_defaults_to_text_only(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    from plugins.spatial_patrol.spatial import _main_model_text_only

    assert _main_model_text_only(_app_config("deepseek-v4-flash", None)) is True


def test_unavailable_config_is_text_only(monkeypatch):
    monkeypatch.setenv("FOCUS_MODEL", "不存在的模型")
    from plugins.spatial_patrol.spatial import _main_model_text_only

    assert _main_model_text_only(_app_config("deepseek-v4-flash", True)) is True


# === 随本变更声明的默认模型 ===


def test_shipped_config_declares_vision_model(monkeypatch):
    monkeypatch.delenv("FOCUS_MODEL", raising=False)
    from focus.config.app_config import reload_app_config

    config = reload_app_config("config.yaml")
    default = config.get_model(config.resolve_default_model_name())
    assert default.supports_image_input is True
    assert config.get_model("deepseek-v4-flash").supports_image_input is False


@pytest.mark.parametrize("edge", [0, -1])
def test_non_positive_edge_falls_back(edge):
    assert estimate_image_tokens(edge, 512) > 0
