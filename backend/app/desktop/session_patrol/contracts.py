"""本文件对外提供 AuthoringDocument、AuthoringEntry、Transformation 和 legacy_document。

输入为任意角色内容、未知字段和未完成 JSON，输出为版本化可保存文档及稳定内容 hash。
工作流为只校验编辑身份，不校验 Provider 协议；旧数据读时适配，原载荷保持不变。
示例：AuthoringDocument(entries=[AuthoringEntry(entry_id="e1", role="developer", content="逐条验证")])。
"""

from copy import deepcopy
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from focus.history import content_hash


class AuthoringEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    role: str = "user"
    content: Any = ""
    source_ref: str | None = None
    source_hash: str | None = None
    edited_from: str | None = None
    copied_from: str | None = None
    reference_only: bool = False


class Transformation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_hash: str
    entry_ids: list[str]
    operation: Literal["as_text", "placeholder", "rename_call"]
    parameters: dict[str, Any] = Field(default_factory=dict)


class AuthoringDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[2] = 2
    entries: list[AuthoringEntry] = Field(default_factory=list)
    instructions: str = ""
    raw_buffer: str | None = None
    raw_error: str | None = None
    transformations: list[Transformation] = Field(default_factory=list)

    @property
    def content_hash(self) -> str:
        value = self.model_dump(mode="json")
        value.pop("transformations", None)
        return content_hash(value)


def legacy_document(system_prompt: str, messages: list[dict], final_message: str = "") -> AuthoringDocument:
    records = deepcopy(messages)
    if final_message:
        records.append({"role": "human", "content": final_message})
    return AuthoringDocument(instructions=system_prompt, entries=[
        AuthoringEntry.model_validate({**record, "entry_id": "legacy:" + content_hash([index, record]),
                                      "legacy_entry_hash": content_hash({key: record.get(key) for key in ("role", "content", "tool_calls", "tool_call_id", "name", "status")})})
        for index, record in enumerate(records)
    ])
