"""
本文件对外提供 create_chat_model 函数。

输入:
    name: 目标模型名，对应 ModelConfig.name。None 时取显式声明的默认模型
          （FOCUS_MODEL 环境变量优先，其次 config.yaml 中 default: true 的条目）
    app_config: 配置对象。None 时内部调用 get_app_config() 自动加载默认 config.yaml
    **kwargs: 透传给 ChatModel 构造器的额外参数（如 temperature、max_tokens），若与 ModelConfig
              映射的参数重名则 **kwargs 优先

输出:
    BaseChatModel 实例

工作流:
    1. 若 app_config 为 None，调用 get_app_config("config.yaml") 自动加载
    2. 若 name 为 None，经 app_config.resolve_default_model_name() 取默认模型名
    3. 否则遍历 app_config.models 按 name 匹配，未命中抛 ValueError
    4. 调用 resolve_class(model_config.use) 获取 ChatModel 类
    5. 以 model/api_key/base_url 为基础参数，合并 **kwargs（kwargs 优先），构造并返回实例

示例:
    create_chat_model() → 默认模型 ChatOpenAI 实例
    create_chat_model("deepseek-v4-flash", temperature=0.5) → 指定模型 + 额外温度参数
"""

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from focus.config import AppConfig, get_app_config
from focus.config.env import require_env_var
from focus.models.http_clients import (
    ProxyFirstAsyncHttpClient,
    ProxyFirstHttpClient,
    environment_proxy_configured,
)
from focus.reflection.resolvers import resolve_class


def create_chat_model(
    name: str | None = None,
    *,
    app_config: AppConfig | None = None,
    **kwargs,
) -> BaseChatModel:
    if app_config is None:
        app_config = get_app_config("config.yaml")

    if name is None:
        name = app_config.resolve_default_model_name()

    for m in app_config.models:
        if m.name == name:
            model_config = m
            break
    else:
        raise ValueError(f"未找到模型配置: '{name}'")

    chat_model_cls = resolve_class(model_config.use)

    params: dict = {
        "model": model_config.model,
        # 密钥在此（唯一消费点）解析：未设置时抛带模型名的 ValueError，而非把引用字面量传下去
        "api_key": require_env_var(model_config.api_key, context=f"模型 '{model_config.name}'"),
        "base_url": model_config.base_url,
    }
    params.update(kwargs)

    # OpenAI 兼容模型在代理环境下优先遵循用户代理；代理发生传输错误时由
    # 客户端透明回退到 trust_env=False 的直连。调用方显式传入网络客户端
    # 或 openai_proxy 时保持其选择，不覆盖。
    caller_controls_network = any(
        key in kwargs for key in ("http_client", "http_async_client", "openai_proxy")
    )
    if (
        issubclass(chat_model_cls, ChatOpenAI)
        and environment_proxy_configured()
        and not caller_controls_network
    ):
        params["http_client"] = ProxyFirstHttpClient()
        params["http_async_client"] = ProxyFirstAsyncHttpClient()

    return chat_model_cls(**params)
