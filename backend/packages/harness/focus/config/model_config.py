"""本文件对外提供 ModelConfig 声明式模型条目。

输入为模型身份、凭据引用、显式 Provider/protocol 和能力；输出为经过字段验证的配置对象。
具体工作流为解析目录字段，保留 legacy adapter 的缺省协议入口，并要求策展默认声明输出合同。
示例：ModelConfig(..., provider="deepseek", protocol="responses", supports_image_input=False)。
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

LEGACY_MODEL_PROVIDERS = {"langchain_openai:ChatOpenAI": "openai", "focus.models.deepseek:DeepSeekChatOpenAI": "deepseek"}


class ModelConfig(BaseModel):
    name: str
    display_name: str
    use: str
    model: str
    api_key: str
    base_url: str
    provider: Literal["openai", "deepseek"] | None = None
    protocol: Literal["responses", "chat_completions"] | None = None
    context_window: int | None = None
    curation_output_method: Literal["json_schema", "json_mode", "prompt_json"] | None = None
    curation_max_output_tokens: int = Field(default=8192, ge=512, le=65536)
    curation_default: bool = False
    default: bool = False
    """显式声明该条目为默认模型；取代原先依赖列表顺序的 models[0] 约定。"""

    supports_image_input: bool = False
    """显式声明该条目是否具备图像输入能力；未声明按不具备处理，取代依据模型名前缀推断。"""

    @model_validator(mode="after")
    def validate_curation_default(self) -> "ModelConfig":
        if self.curation_default and self.curation_output_method is None:
            raise ValueError("策展默认模型必须声明 curation_output_method")
        return self
