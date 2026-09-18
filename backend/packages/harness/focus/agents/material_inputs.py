"""本文件对外提供 RunMaterialInput、RunMaterialInputs 与统一运行材料上下文投影函数。

输入为服务端已验证的材料快照、稳定 origin run/message 身份和独立 required 图片 ID；输出为
不可变运行材料聚合、单向兼容读取的 equipment JSON、派生 RunImageInputs、<current_uploads> 标签
及 runtime context。具体工作流为保留用户顺序和备注原文，拒绝重复材料与非图片必看项，新写入只使用
run_material_inputs（并在此声明它作为 run_image_inputs / model_supports_image_input 两个受治理键的
服务端生产者）；旧 run_image_inputs 或 must_view_materials 仅在读取时提升为通用结构。

示例：inputs = RunMaterialInputs.build("r1", "msg1", materials, ["image1"])。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from focus.agents.image_inputs import (
    MODEL_IMAGE_INPUT_KEY,
    RUN_IMAGE_INPUTS_CONTEXT_KEY,
    RunImageInput,
    RunImageInputs,
    project_run_image_context,
)
from focus.security.governed import declare_governed_producer


RUN_MATERIAL_INPUTS_KEY = "run_material_inputs"

PRODUCER = "focus.agents.material_inputs.project_run_material_context"
"""本模块作为服务端生产者的稳定标识。"""

# 材料与图片投影是这两个受治理键的唯一服务端生产者：它们只在本模块写回运行上下文
declare_governed_producer(RUN_IMAGE_INPUTS_CONTEXT_KEY, PRODUCER)
declare_governed_producer(MODEL_IMAGE_INPUT_KEY, PRODUCER)


@dataclass(frozen=True, slots=True)
class RunMaterialInput:
    material_id: str
    relative_path: str
    digest: str
    material_kind: str
    delivery_mode: str
    reading_mode: str
    instruction_mode: str
    size_bytes: int
    note: str = ""
    source_bytes: int = 0
    model_bytes: int = 0
    model_tokens: int = 0
    snapshot_object_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunMaterialInput":
        required = ("material_id", "relative_path", "digest", "material_kind")
        if any(not str(value.get(field) or "") for field in required):
            raise ValueError("运行材料缺少 material_id、relative_path、digest 或 material_kind")
        return cls(
            material_id=str(value["material_id"]),
            relative_path=str(value["relative_path"]),
            digest=str(value["digest"]),
            material_kind=str(value["material_kind"]),
            delivery_mode=str(value.get("delivery_mode") or "reference"),
            reading_mode=str(value.get("reading_mode") or "full"),
            instruction_mode=str(value.get("instruction_mode") or "reference"),
            size_bytes=max(0, int(value.get("size_bytes") or 0)),
            note=str(value.get("note") or ""),
            source_bytes=max(0, int(value.get("source_bytes") or 0)),
            model_bytes=max(0, int(value.get("model_bytes") or 0)),
            model_tokens=max(0, int(value.get("model_tokens") or 0)),
            snapshot_object_id=(str(value.get("snapshot_object_id")) if value.get("snapshot_object_id") else None),
        )

    def to_json(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class RunMaterialInputs:
    origin_run_id: str = ""
    origin_message_id: str = ""
    attached: tuple[RunMaterialInput, ...] = ()
    required_image_ids: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "RunMaterialInputs":
        return cls()

    @classmethod
    def build(
        cls,
        origin_run_id: str,
        origin_message_id: str,
        attached: Iterable[RunMaterialInput | Mapping[str, Any]],
        required_image_ids: Iterable[str] = (),
    ) -> "RunMaterialInputs":
        items = tuple(
            item if isinstance(item, RunMaterialInput) else RunMaterialInput.from_mapping(item)
            for item in attached
        )
        ids = [item.material_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("运行材料不能重复")
        required = tuple(dict.fromkeys(str(value) for value in required_image_ids if str(value)))
        by_id = {item.material_id: item for item in items}
        detached = [value for value in required if value not in by_id]
        if detached:
            raise ValueError(f"必须看图片必须先选择: {', '.join(detached)}")
        non_images = [value for value in required if by_id[value].material_kind != "image"]
        if non_images:
            raise ValueError(f"只有图片可以标记为必须看: {', '.join(non_images)}")
        return cls(origin_run_id, origin_message_id, items, required)

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> "RunMaterialInputs":
        if not isinstance(value, Mapping):
            return cls.empty()
        return cls.build(
            str(value.get("origin_run_id") or ""),
            str(value.get("origin_message_id") or ""),
            value.get("attached") if isinstance(value.get("attached"), list) else (),
            value.get("required_image_ids") if isinstance(value.get("required_image_ids"), list) else (),
        )

    @classmethod
    def from_equipment(cls, equipment: Mapping[str, Any] | None) -> "RunMaterialInputs":
        if not isinstance(equipment, Mapping):
            return cls.empty()
        current = equipment.get(RUN_MATERIAL_INPUTS_KEY)
        if isinstance(current, Mapping):
            return cls.from_json(current)
        images = RunImageInputs.from_equipment(equipment)
        items = [
            RunMaterialInput(
                material_id=item.material_id,
                relative_path=item.relative_path,
                digest=item.digest or "legacy",
                material_kind="image",
                delivery_mode="image",
                reading_mode="full",
                instruction_mode="reference",
                size_bytes=item.source_bytes,
                source_bytes=item.source_bytes,
                model_bytes=item.model_bytes,
                model_tokens=item.model_tokens,
                snapshot_object_id=item.snapshot_object_id,
            )
            for item in images.attached
        ]
        return cls.build("", "", items, images.required_ids)

    @classmethod
    def from_context(cls, context: Any) -> "RunMaterialInputs":
        if not isinstance(context, Mapping):
            return cls.empty()
        current = context.get(RUN_MATERIAL_INPUTS_KEY)
        if isinstance(current, Mapping):
            return cls.from_json(current)
        return cls.from_equipment(context)

    @property
    def images(self) -> RunImageInputs:
        return RunImageInputs.build(
            (
                RunImageInput(
                    material_id=item.material_id,
                    relative_path=item.relative_path,
                    source_bytes=item.source_bytes,
                    model_bytes=item.model_bytes,
                    model_tokens=item.model_tokens,
                    snapshot_object_id=item.snapshot_object_id,
                    digest=item.digest,
                )
                for item in self.attached
                if item.material_kind == "image"
            ),
            self.required_image_ids,
        )

    @property
    def uploads_tag(self) -> str:
        """本轮已选材料的 <current_uploads> 标签；无材料时为空串。

        这是该标签的唯一构造点：桌面材料的策略投影与承诺子图的上传清单都取自它，
        避免同一份事实在两处各写一遍格式。
        """
        paths = [item.relative_path for item in self.attached]
        if not paths:
            return ""
        return "<current_uploads>\n" + "\n".join(paths) + "\n</current_uploads>"

    def to_json(self) -> dict[str, Any]:
        return {
            "origin_run_id": self.origin_run_id,
            "origin_message_id": self.origin_message_id,
            "attached": [item.to_json() for item in self.attached],
            "required_image_ids": list(self.required_image_ids),
        }

    def to_equipment(self) -> dict[str, Any]:
        return {RUN_MATERIAL_INPUTS_KEY: self.to_json()}


def project_run_material_context(
    context: dict[str, Any], inputs: RunMaterialInputs, model_supports_images: bool
) -> dict[str, Any]:
    context[RUN_MATERIAL_INPUTS_KEY] = inputs.to_json()
    project_run_image_context(context, inputs.images, model_supports_images)
    return context
