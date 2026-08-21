"""spatial-patrol 插件的视觉模型装配(硬性要求)。

对外提供:
    resolve_vision_model(plugin_config) — 按插件 config.json 的 vision_model 条目名
        从 AppConfig.models 装配视觉模型;缺失时抛 RuntimeError(loader 据此标记
        插件 Unavailable)

输入:
    plugin_config: dict — plugins/spatial-patrol/config.json 原样内容

工作流:
    (1) vision_model 条目名为空 → RuntimeError(缺少视觉模型配置)
    (2) create_chat_model(name=...) 未命中 → RuntimeError(说明缺哪个条目)
"""

from langchain_core.language_models import BaseChatModel

from focus.config import get_app_config
from focus.models import create_chat_model


def resolve_vision_model(plugin_config: dict) -> BaseChatModel:
    """装配视觉模型;系统模型为 text-only(如 DeepSeek V4)时这是空间观察的硬性前提。"""
    name = str((plugin_config or {}).get("vision_model") or "").strip()
    if not name:
        raise RuntimeError(
            "缺少视觉模型配置:插件 config.json 的 vision_model 为空"
        )
    try:
        return create_chat_model(name=name, app_config=get_app_config("config.yaml"))
    except ValueError as exc:
        raise RuntimeError(
            f"缺少视觉模型配置:config.yaml models 中不存在条目 '{name}'"
        ) from exc
