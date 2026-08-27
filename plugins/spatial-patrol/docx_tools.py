"""The only Agent-facing tools for active rich DOCX editor sessions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from plugins.spatial_patrol.docx_broker import BrokerError, broker


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplaceText(_Arguments):
    text: str


class InsertText(_Arguments):
    text: str
    position: Literal["before", "after", "inside_start", "inside_end"] = "after"


class EmptyArguments(_Arguments):
    pass


class TextFormat(_Arguments):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikeout: bool | None = None
    font_name: str | None = Field(default=None, max_length=128)
    font_size: float | None = Field(default=None, ge=1, le=300)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class ParagraphFormat(_Arguments):
    alignment: Literal["left", "center", "right", "justify"] | None = None
    line_spacing: float | None = Field(default=None, ge=0.5, le=10)
    spacing_before: float | None = Field(default=None, ge=0, le=1584)
    spacing_after: float | None = Field(default=None, ge=0, le=1584)
    indent_left: float | None = Field(default=None, ge=-1584, le=1584)
    indent_right: float | None = Field(default=None, ge=-1584, le=1584)
    first_line: float | None = Field(default=None, ge=-1584, le=1584)
    keep_with_next: bool | None = None
    page_break_before: bool | None = None


class ApplyStyle(_Arguments):
    style_name: str = Field(min_length=1, max_length=128)


class ListFormat(_Arguments):
    kind: Literal["none", "bullet", "numbered"]
    level: int = Field(default=0, ge=0, le=8)


class TableMutation(_Arguments):
    index: int | None = Field(default=None, ge=0)
    count: int = Field(default=1, ge=1, le=100)


class MergeCells(_Arguments):
    start_row: int = Field(ge=0)
    start_column: int = Field(ge=0)
    end_row: int = Field(ge=0)
    end_column: int = Field(ge=0)


class DrawingMutation(_Arguments):
    source_url: str | None = None
    alt_text: str | None = Field(default=None, max_length=1024)
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    wrap: Literal["inline", "square", "tight", "behind", "in_front"] | None = None


class PageLayout(_Arguments):
    orientation: Literal["portrait", "landscape"] | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    margin_top: float | None = Field(default=None, ge=0)
    margin_right: float | None = Field(default=None, ge=0)
    margin_bottom: float | None = Field(default=None, ge=0)
    margin_left: float | None = Field(default=None, ge=0)
    columns: int | None = Field(default=None, ge=1, le=12)


class HeaderFooter(_Arguments):
    kind: Literal["header", "footer"]
    text: str
    first_page: bool = False
    odd_even: Literal["all", "odd", "even"] = "all"


class Watermark(_Arguments):
    text: str | None = Field(default=None, max_length=256)
    image_url: str | None = None
    opacity: float = Field(default=0.35, ge=0, le=1)


class Comment(_Arguments):
    text: str = Field(min_length=1, max_length=10000)


class TrackChanges(_Arguments):
    enabled: bool


OPERATION_ARGUMENTS: dict[str, type[_Arguments]] = {
    "replace_text": ReplaceText,
    "insert_text": InsertText,
    "delete_target": EmptyArguments,
    "set_text_format": TextFormat,
    "set_paragraph_format": ParagraphFormat,
    "apply_style": ApplyStyle,
    "set_list": ListFormat,
    "insert_table_row": TableMutation,
    "delete_table_row": TableMutation,
    "insert_table_column": TableMutation,
    "delete_table_column": TableMutation,
    "merge_cells": MergeCells,
    "split_cell": TableMutation,
    "insert_drawing": DrawingMutation,
    "update_drawing": DrawingMutation,
    "delete_drawing": EmptyArguments,
    "set_page_layout": PageLayout,
    "set_header_footer": HeaderFooter,
    "set_watermark": Watermark,
    "remove_watermark": EmptyArguments,
    "add_comment": Comment,
    "delete_comment": EmptyArguments,
    "set_track_changes": TrackChanges,
    "accept_revision": EmptyArguments,
    "reject_revision": EmptyArguments,
}


def _context(runtime: ToolRuntime) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        raise ToolException("缺少 DOCX 编辑器上下文")
    required = {"docx_session_id", "docx_document_id", "docx_document_version"}
    missing = sorted(required - context.keys())
    if missing:
        raise ToolException(f"缺少 DOCX 上下文字段: {', '.join(missing)}")
    return context


def _record_evidence(context: dict[str, Any], evidence: dict[str, Any]) -> None:
    target = context.get("docx_change_evidence")
    if isinstance(target, dict):
        target.clear()
        target.update(evidence)


@tool
async def observe_docx_target(target_id: str, runtime: ToolRuntime = None) -> dict[str, Any]:
    """Observe one semantic editor target: content, formatting, type, version and page projection. Start with the anchor target; do not read the whole document."""
    context = _context(runtime)
    session_id = str(context["docx_session_id"])
    try:
        try:
            observed = broker.observe(session_id, target_id)
        except BrokerError:
            result = await broker.issue(
                session_id,
                action="observe",
                target_id=target_id,
                expected_version=int(context["docx_document_version"]),
            )
            observed = result.get("observation")
            if not isinstance(observed, dict):
                raise BrokerError("编辑器没有返回目标观察结果")
        return observed
    except BrokerError as exc:
        raise ToolException(str(exc)) from exc


@tool
async def apply_docx_edit(
    target_id: str,
    operation: str,
    arguments: dict[str, Any],
    runtime: ToolRuntime = None,
) -> dict[str, Any]:
    """Apply one validated rich Word operation to one semantic target as one undoable editor history point."""
    context = _context(runtime)
    if "write" not in frozenset(context.get("permissions") or []):
        raise ToolException("当前 DOCX 会话未授权 write")
    model = OPERATION_ARGUMENTS.get(operation)
    if not model:
        raise ToolException(f"不支持的 DOCX 操作: {operation}")
    try:
        validated = model.model_validate(arguments).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ToolException(f"DOCX 操作参数无效: {exc}") from exc
    version = int(context["docx_document_version"])
    failure_key = hashlib.sha256(json.dumps(
        [target_id, operation, validated, version], sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
    previous = context.get("docx_change_evidence")
    if isinstance(previous, dict) and previous.get("failure_key") == failure_key:
        raise ToolException("相同 DOCX 操作已被确定性拒绝，请先重新观察目标")
    try:
        result = await broker.issue(
            str(context["docx_session_id"]),
            action="edit",
            target_id=target_id,
            operation=operation,
            arguments=validated,
            expected_version=version,
        )
    except BrokerError as exc:
        evidence = {"changed": False, "error": str(exc), "failure_key": failure_key}
        _record_evidence(context, evidence)
        raise ToolException(str(exc)) from exc
    changed = result.get("changed") is True
    result_version = result.get("document_version")
    if changed and (not isinstance(result_version, int) or result_version <= version):
        evidence = {
            "changed": False,
            "error": "编辑器返回的版本未递增",
            "failure_key": failure_key,
        }
        _record_evidence(context, evidence)
        raise ToolException(evidence["error"])
    evidence = {
        **result,
        "changed": changed,
        "target_id": target_id,
        "operation": operation,
        "before_version": version,
        "after_version": result_version,
    }
    _record_evidence(context, evidence)
    if changed:
        context["docx_document_version"] = result_version
    return evidence


def _recoverable(error: ToolException) -> str:
    return f"DOCX 编辑未执行：{error}"


observe_docx_target.handle_tool_error = _recoverable
apply_docx_edit.handle_tool_error = _recoverable
