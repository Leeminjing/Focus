"""Patrol 决策的独立模型、输出合同和首次证据容量。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PatrolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str | None = None
    output_method: Literal["json_schema", "json_mode", "prompt_json"] = "prompt_json"
    max_output_tokens: int = Field(default=2048, ge=512, le=65536)
    streaming: bool = True
    initial_evidence_max_bytes: int = Field(default=16384, ge=0, le=262144)
