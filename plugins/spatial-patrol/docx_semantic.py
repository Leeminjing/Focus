"""DOCX 语义锚点协议。

输入为 Bridge 已解析的页面 placement、可选语义目标与当前投影；输出为可存入
``SpatialAnchor.region`` 的严格 JSON。工作流始终保留用户点击的页面点，内容目标
只负责重排恢复，页面空白则以 ``page_region`` 本身作为空间真源。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DOCX_COORDINATE_SPACE = "docx-semantic-v1"
TARGET_KINDS = frozenset({
    "text_range", "paragraph", "table", "cell", "image", "shape",
    "header", "footer", "page_region", "selection",
})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StructureStep(_Strict):
    kind: str = Field(min_length=1)
    index: int = Field(ge=0)
    key: str | None = None


class TargetFingerprint(_Strict):
    exact: str = Field(default="", max_length=1024)
    prefix: str = Field(default="", max_length=256)
    suffix: str = Field(default="", max_length=256)
    style: str | None = Field(default=None, max_length=128)


class SemanticTarget(_Strict):
    target_id: str = Field(min_length=1, max_length=256)
    kind: str
    stable_id: str | None = Field(default=None, max_length=256)
    structure_path: list[StructureStep] = Field(default_factory=list)
    fingerprint: TargetFingerprint

    @model_validator(mode="after")
    def supported_kind(self):
        if self.kind not in TARGET_KINDS:
            raise ValueError(f"unsupported DOCX target kind: {self.kind}")
        return self


class PageRect(_Strict):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def inside_page(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("projection rectangle leaves the page")
        return self


class TargetProjection(_Strict):
    page: int = Field(ge=1)
    rect: PageRect | None = None
    projection_unavailable: bool = False

    @model_validator(mode="after")
    def has_projection_or_reason(self):
        if self.rect is None and not self.projection_unavailable:
            raise ValueError("projection must contain a rect or be unavailable")
        return self


class PagePlacement(_Strict):
    page: int = Field(ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class TargetAttachment(_Strict):
    page_offset: int = Field(ge=0)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class DocxSemanticRegion(_Strict):
    coordinate_space: Literal["docx-semantic-v1"] = DOCX_COORDINATE_SPACE
    document_id: str = Field(min_length=1, max_length=64)
    document_version: int = Field(ge=1)
    placement: PagePlacement
    attachment: TargetAttachment | None = None
    target: SemanticTarget
    projection: TargetProjection

    @model_validator(mode="after")
    def placement_matches_projection(self):
        if self.placement.page != self.projection.page:
            raise ValueError("placement page must match projection page")
        if self.target.kind == "page_region" and self.projection.rect is not None:
            rect = self.projection.rect
            if (
                abs(rect.x - self.placement.x) > 0.000001
                or abs(rect.y - self.placement.y) > 0.000001
                or rect.width != 0
                or rect.height != 0
            ):
                raise ValueError("page_region projection must be the exact placement point")
        return self


def serialize_semantic_region(region: DocxSemanticRegion) -> dict[str, Any]:
    return region.model_dump(mode="json")
