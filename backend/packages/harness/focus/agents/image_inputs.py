"""本文件对外提供 RunImageInput、RunImageInputs 与统一的 run 图片上下文投影函数。

输入为服务端已验证的图片材料描述、必看材料标识和模型图像能力；材料描述中的
material_id 是稳定身份，relative_path 是工作区相对路径，model_bytes/model_tokens 是
该图片实际送模副本的资源估算，source_bytes 是本轮解析时的原文件边界，snapshot_object_id
是当前路径变化后可用的受保护 Git blob。输出为不可变的
RunImageInputs、可持久化 equipment JSON
以及供 middleware 读取的 runtime context。

具体工作流为：按输入顺序去重附件，校验 required 是 attached 的子集并聚合送模预算；
通用 RunMaterialInputs 从中派生图片视图，新运行只把该视图投影到 runtime context，旧
run_image_inputs 与 must_view_materials 则由通用聚合兼容读取；图片 middleware 不查询材料 ID。

示例：inputs = RunImageInputs.build(materials, ["m1"]); equipment.update(inputs.to_equipment())。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

RUN_IMAGE_INPUTS_EQUIPMENT_KEY = "run_image_inputs"
RUN_IMAGE_INPUTS_CONTEXT_KEY = "run_image_inputs"
MODEL_IMAGE_INPUT_KEY = "model_supports_image_input"


@dataclass(frozen=True, slots=True)
class RunImageInput:
    material_id: str
    relative_path: str
    source_bytes: int = 0
    model_bytes: int = 0
    model_tokens: int = 0
    snapshot_object_id: str | None = None
    digest: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunImageInput":
        material_id = str(value.get("material_id") or "")
        relative_path = str(value.get("relative_path") or "")
        if not material_id or not relative_path:
            raise ValueError("图片材料描述必须包含 material_id 与 relative_path")
        return cls(
            material_id=material_id,
            relative_path=relative_path,
            source_bytes=max(0, int(value.get("source_bytes") or 0)),
            model_bytes=max(0, int(value.get("model_bytes") or 0)),
            model_tokens=max(0, int(value.get("model_tokens") or 0)),
            snapshot_object_id=(str(value.get("snapshot_object_id")) if value.get("snapshot_object_id") else None),
            digest=str(value.get("digest") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "relative_path": self.relative_path,
            "source_bytes": self.source_bytes,
            "model_bytes": self.model_bytes,
            "model_tokens": self.model_tokens,
            "snapshot_object_id": self.snapshot_object_id,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class RunImageInputs:
    attached: tuple[RunImageInput, ...] = ()
    required_ids: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "RunImageInputs":
        return cls()

    @classmethod
    def build(
        cls,
        attached: Iterable[RunImageInput | Mapping[str, Any]],
        required_ids: Iterable[str] = (),
    ) -> "RunImageInputs":
        unique: list[RunImageInput] = []
        seen: set[str] = set()
        for raw in attached:
            item = raw if isinstance(raw, RunImageInput) else RunImageInput.from_mapping(raw)
            if item.material_id in seen:
                continue
            seen.add(item.material_id)
            unique.append(item)
        required = tuple(dict.fromkeys(str(value) for value in required_ids if str(value)))
        detached = [material_id for material_id in required if material_id not in seen]
        if detached:
            raise ValueError(f"必须看图片必须先附加: {', '.join(detached)}")
        return cls(attached=tuple(unique), required_ids=required)

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> "RunImageInputs":
        if not isinstance(value, Mapping):
            return cls.empty()
        attached = value.get("attached")
        required = value.get("required_ids")
        return cls.build(
            attached if isinstance(attached, list) else (),
            required if isinstance(required, list) else (),
        )

    @classmethod
    def from_equipment(cls, equipment: Mapping[str, Any] | None) -> "RunImageInputs":
        if not isinstance(equipment, Mapping):
            return cls.empty()
        current = equipment.get(RUN_IMAGE_INPUTS_EQUIPMENT_KEY)
        if isinstance(current, Mapping):
            return cls.from_json(current)
        legacy = equipment.get("must_view_materials")
        if isinstance(legacy, list):
            items = [item for item in legacy if isinstance(item, Mapping)]
            return cls.build(items, [str(item.get("material_id") or "") for item in items])
        return cls.empty()

    @classmethod
    def from_context(cls, context: Any) -> "RunImageInputs":
        if not isinstance(context, Mapping):
            return cls.empty()
        current = context.get(RUN_IMAGE_INPUTS_CONTEXT_KEY)
        if isinstance(current, Mapping):
            return cls.from_json(current)
        legacy = context.get("must_view_materials")
        if isinstance(legacy, list):
            items = [item for item in legacy if isinstance(item, Mapping)]
            return cls.build(items, [str(item.get("material_id") or "") for item in items])
        return cls.empty()

    @property
    def required(self) -> tuple[RunImageInput, ...]:
        required = set(self.required_ids)
        return tuple(item for item in self.attached if item.material_id in required)

    @property
    def total_model_bytes(self) -> int:
        return sum(item.model_bytes for item in self.attached)

    @property
    def total_model_tokens(self) -> int:
        return sum(item.model_tokens for item in self.attached)

    def to_json(self) -> dict[str, Any]:
        return {
            "attached": [item.to_json() for item in self.attached],
            "required_ids": list(self.required_ids),
            "total_model_bytes": self.total_model_bytes,
            "total_model_tokens": self.total_model_tokens,
        }

    def to_equipment(self) -> dict[str, Any]:
        return {RUN_IMAGE_INPUTS_EQUIPMENT_KEY: self.to_json()}


def project_run_image_context(
    context: dict[str, Any], inputs: RunImageInputs, model_supports_images: bool
) -> dict[str, Any]:
    context[RUN_IMAGE_INPUTS_CONTEXT_KEY] = inputs.to_json()
    context[MODEL_IMAGE_INPUT_KEY] = bool(model_supports_images)
    return context
