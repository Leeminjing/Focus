"""本文件对外提供 AuthoringDocument、AuthoringEntry、Transformation、SourcePreviewRequest 和 legacy_document。

输入为 kind/payload、稳定编辑身份、任意角色及未知字段和未完成 JSON，输出为 v3 可保存文档及稳定内容 hash。
工作流为只校验编辑身份，不校验 Provider 协议；旧数据读时适配，原载荷保持不变；来源预览请求限制分页范围，不提供草稿写入能力。
示例：AuthoringDocument(entries=[AuthoringEntry(entry_id="e1", kind="message", payload={"role":"developer","content":"逐条验证"})])。
"""

from copy import deepcopy
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from focus.history import content_hash
from .authoring_adapters import upgrade_document, expand_record


class AuthoringEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    kind: str
    payload: dict[str, Any]
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


class SourcePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["context", "patrol", "file", "material"]
    query: str = Field(default="", max_length=1000)
    cursor: str | None = Field(default=None, max_length=4096)
    row_id: str | None = None
    limit: int = Field(default=50, ge=1, le=100)
    include_historical: bool = False


class AuthoringDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[3] = 3
    entries: list[AuthoringEntry] = Field(default_factory=list)
    instructions: str = ""
    raw_buffer: str | None = None
    raw_error: str | None = None
    transformations: list[Transformation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_document(cls, value):
        return upgrade_document(value) if isinstance(value, dict) else value

    @property
    def content_hash(self) -> str:
        value = self.model_dump(mode="json")
        value.pop("transformations", None)
        return content_hash(value)


def legacy_document(system_prompt: str, messages: list[dict], final_message: str = "") -> AuthoringDocument:
    records = deepcopy(messages)
    if final_message:
        records.append({"role": "human", "content": final_message})
    entries = [entry for index, record in enumerate(records)
               for entry in expand_record(record, "legacy:" + content_hash([index, record]))]
    for entry in entries:
        entry["legacy_entry_hash"] = content_hash([entry["kind"], entry["payload"]])
    return AuthoringDocument(instructions=system_prompt, entries=entries)
