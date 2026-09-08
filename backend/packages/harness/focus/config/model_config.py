from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    name: str
    display_name: str
    use: str
    model: str
    api_key: str
    base_url: str
    context_window: int | None = None
    curation_output_method: Literal["json_schema", "json_mode", "prompt_json"] | None = None
    curation_max_output_tokens: int = Field(default=8192, ge=512, le=65536)
    curation_default: bool = False

    @model_validator(mode="after")
    def validate_curation_default(self) -> "ModelConfig":
        if self.curation_default and self.curation_output_method is None:
            raise ValueError("策展默认模型必须声明 curation_output_method")
        return self
