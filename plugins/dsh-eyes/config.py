"""dsh-eyes 插件配置:读取 config.json 并解析 $ENV 引用(还原 dsh-eyes 的 env 配置方式)。

对外提供:
    load_config() -> dict — 原样内容 + $VAR 解析(仅 vision_api_key 等字符串字段)
    resolve_api_key(config) -> str — 解析后的视觉 API key;缺失抛 RuntimeError
    endpoint_url(config) -> str — 协议自适应:以 /chat/completions 结尾直用,否则拼接
"""

import os
import re
from pathlib import Path

_ENV_PATTERN = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _resolve_env(value):
    """$VAR → os.environ 值;缺失抛 RuntimeError(插件自身配置问题)。"""
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.match(value)
    if not match:
        return value
    name = match.group(1)
    if name not in os.environ:
        raise RuntimeError(f"缺少环境变量: {name}(插件配置引用了未设置的变量)")
    return os.environ[name]


def load_config() -> dict:
    """读取插件 config.json 并解析环境变量引用。"""
    import json

    raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {key: _resolve_env(value) for key, value in raw.items()}


def resolve_api_key(config: dict) -> str:
    key = str(config.get("vision_api_key") or "").strip()
    if not key:
        raise RuntimeError("缺少视觉 API key:config.json 的 vision_api_key 为空")
    return key


_CHAT_PATH = "/chat/completions"
_RESPONSES_PATH = "/responses"


def _suffix_of(url: str) -> str:
    """端点已携带的协议后缀;'' 表示裸 base URL(对齐 dsh-eyes suffixOf)。"""
    u = (url or "").strip().rstrip("/")
    if u.lower().endswith(_RESPONSES_PATH):
        return _RESPONSES_PATH
    if u.lower().endswith(_CHAT_PATH):
        return _CHAT_PATH
    return ""


def resolve_style(config: dict) -> str:
    """有效 API 风格:显式 'chat'/'responses' 优先;'auto' 按端点后缀识别。"""
    style = str(config.get("vision_api_style", "auto")).lower()
    if style in ("chat", "responses"):
        return style
    return "responses" if _suffix_of(str(config.get("vision_endpoint", ""))) == _RESPONSES_PATH else "chat"


def endpoint_url(config: dict) -> str:
    """endpoint 规范化到当前风格的确切路径(对齐 dsh-eyes resolveEndpoint)。"""
    endpoint = str(config.get("vision_endpoint") or "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("缺少视觉端点:config.json 的 vision_endpoint 为空")
    suffix = _suffix_of(endpoint)
    base = endpoint[: -len(suffix)] if suffix else endpoint
    return base + (_RESPONSES_PATH if resolve_style(config) == "responses" else _CHAT_PATH)


def endpoint_for_style(config: dict, style: str) -> str:
    """指定风格下的确切端点路径(请求层使用,跳过重复解析)。"""
    endpoint = str(config.get("vision_endpoint") or "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("缺少视觉端点:config.json 的 vision_endpoint 为空")
    suffix = _suffix_of(endpoint)
    base = endpoint[: -len(suffix)] if suffix else endpoint
    return base + (_RESPONSES_PATH if style == "responses" else _CHAT_PATH)


def vision_model(config: dict) -> str:
    model = str(config.get("vision_model") or "").strip()
    if not model:
        raise RuntimeError("缺少视觉模型:config.json 的 vision_model 为空")
    return model


def max_image_bytes(config: dict) -> int:
    return int(config.get("max_image_bytes", 15728640))


def attachment_max_bytes(config: dict) -> int:
    """粘贴/附件图片上限(对齐 dsh-eyes:Harness 附件存储默认 5 MB,独立于 image_path 上限)。"""
    return int(config.get("attachment_max_bytes", 5242880))


def passthrough_models(config: dict) -> set[str]:
    return {str(name) for name in (config.get("passthrough_models") or [])}
