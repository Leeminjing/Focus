"""本文件对外提供空间运行的受治理上下文生产者 project_spatial_context 与它生产的键清单。

输入为已组装的运行上下文、空间锚点、变更证据、DOCX 观察候选与会话身份；输出为同一上下文（原地写回
空间观察与 DOCX 编辑所读取的受治理键）。具体工作流为由身份投影与统一组装入口先产出基础上下文
（受治理键不得来自调用方载荷），再由本模块作为服务端生产者写回空间坐标、锚点身份与 DOCX 会话身份；
锚点字段缺失时抛错而不是写空值，使"启动点漏填"表现为显式失败。

示例：
    context = assemble_run_context(profile, {"app_config": app_config})
    project_spatial_context(context, anchor, change_evidence=evidence, docx_session=session)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from focus.security.governed import declare_governed_producer

SPATIAL_CONTEXT_KEYS: tuple[str, ...] = (
    "spatial_id",
    "content_ref",
    "page",
    "x",
    "y",
    "docx_change_evidence",
    "docx_observation_candidates",
    "docx_session_id",
    "docx_document_id",
    "docx_document_version",
)
"""本模块生产的受治理键：空间观察的锚点与坐标，以及 DOCX 编辑的会话身份。"""

PRODUCER = "plugins.spatial_patrol.spatial_context.project_spatial_context"
"""本模块作为服务端生产者的稳定标识。"""

for _key in SPATIAL_CONTEXT_KEYS:
    declare_governed_producer(_key, PRODUCER)


def _anchor_value(anchor: Any, field: str) -> Any:
    """取锚点字段；缺失即失败，避免把空坐标带进运行上下文。"""
    value = getattr(anchor, field, None)
    if value is None or value == "":
        raise RuntimeError(f"空间锚点缺少字段: {field}")
    return value


def require_spatial_value(context: object, key: str) -> Any:
    """取一个由本模块生产的受治理键；缺失或为空即显式失败并指明键名。

    输入:
        context: object — 运行上下文
        key: str — 本模块生产的受治理键名

    输出:
        Any — 该键的值（可能是空 dict / None 这类"已声明为空"的合法值）

    工作流:
        (1) 校验上下文是字典，否则显式失败
        (2) 校验键存在（缺失即失败），不以 None/默认值代替
    """
    if not isinstance(context, dict):
        raise RuntimeError("缺少空间上下文: runtime.context 必须为 dict")
    if key not in context:
        raise RuntimeError(f"缺少空间上下文受治理键: {key}")
    return context[key]


def require_carrier_field(context: object, key: str) -> Any:
    """取受治理的载体字段（workspace / content_ref）；缺失或为空即失败并指明该字段。

    输入:
        context: object — 运行上下文
        key: str — 载体字段名

    输出:
        Any — 该字段的非空值

    工作流:
        (1) 校验上下文是映射，否则显式失败
        (2) 校验字段存在且非空（缺失即失败并指明是哪一个），不以默认值代替
    """
    if not isinstance(context, Mapping):
        raise RuntimeError("缺少受治理的载体上下文: runtime.context 必须为映射")
    value = context.get(key)
    if value is None or str(value) == "":
        raise RuntimeError(f"缺少受治理的载体上下文: {key}")
    return value


def project_spatial_context(
    context: dict[str, Any],
    anchor: Any,
    *,
    change_evidence: dict[str, Any] | None = None,
    docx_candidates: dict[str, Any] | None = None,
    docx_session: Any | None = None,
) -> dict[str, Any]:
    """把空间与 DOCX 的受治理键写回运行上下文。

    输入:
        context: dict — 已含身份投影与附带载荷的运行上下文
        anchor: Any — 空间锚点（提供 spatial_id / content_ref / page / x / y）
        change_evidence: dict | None — 本次运行已产生的可验证变更证据
        docx_candidates: dict | None — 本次运行可编辑的 DOCX 目标候选
        docx_session: Any | None — DOCX 只读/可写会话身份（提供 session_id 等）

    输出:
        dict — 同一个 context（原地写回）
    """
    context["spatial_id"] = _anchor_value(anchor, "spatial_id")
    context["content_ref"] = _anchor_value(anchor, "content_ref")
    context["page"] = _anchor_value(anchor, "page")
    context["x"] = _anchor_value(anchor, "x")
    context["y"] = _anchor_value(anchor, "y")
    context["docx_change_evidence"] = change_evidence if change_evidence is not None else {}
    context["docx_observation_candidates"] = docx_candidates if docx_candidates is not None else {}
    context["docx_session_id"] = docx_session.session_id if docx_session else None
    context["docx_document_id"] = docx_session.document_id if docx_session else None
    context["docx_document_version"] = docx_session.document_version if docx_session else None
    return context
