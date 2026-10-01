"""本文件对外提供 ProviderContract 与 resolve_provider_contract。

输入为模型配置的显式 Provider/protocol/图像能力或已识别 legacy adapter；输出为不可变有效协议合同。
具体工作流为固定映射旧格式、拒绝未知或冲突配置，不使用模型名或 HTTP 失败推断能力。
示例：contract = resolve_provider_contract(config)；legacy Chat 仅作为显式兼容协议保留。
"""

from dataclasses import dataclass
from typing import Literal
from focus.config.model_config import LEGACY_MODEL_PROVIDERS


PROJECTION_VERSION = "focus-responses-v1"
_LEGACY = LEGACY_MODEL_PROVIDERS
RESPONSES_ADAPTER = "focus.models.responses:FocusResponsesChatModel"


@dataclass(frozen=True)
class ProviderContract:
    provider: Literal["openai", "deepseek"]
    protocol: Literal["responses", "chat_completions"]
    supports_image_input: bool = False
    context_window: int | None = None
    projection_version: str = PROJECTION_VERSION

    @property
    def policy_role(self):
        return "system" if self.provider == "deepseek" else "developer"


def resolve_provider_contract(config) -> ProviderContract:
    provider = config.provider or _LEGACY.get(config.use)
    if provider is None:
        raise ValueError(f"未知模型适配器需要明确 provider/protocol: {config.use}")
    protocol = config.protocol or ("chat_completions" if config.use in _LEGACY else None)
    if protocol is None:
        raise ValueError("Responses 适配器必须显式声明 protocol")
    if config.use in _LEGACY and config.provider is not None and config.provider != _LEGACY[config.use]:
        raise ValueError("模型 provider 与已识别 adapter 冲突")
    if config.use not in {*_LEGACY, RESPONSES_ADAPTER}:
        raise ValueError(f"未支持的模型适配器: {config.use}")
    if config.use == RESPONSES_ADAPTER and protocol != "responses":
        raise ValueError("Responses adapter 不支持 Chat fallback")
    return ProviderContract(provider, protocol, config.supports_image_input, config.context_window)
