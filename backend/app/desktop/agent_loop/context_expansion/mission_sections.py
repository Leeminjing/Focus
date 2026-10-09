r"""本文件对外提供 FrozenMissionSectionCatalog、MissionSectionEntry 与 MissionSectionMismatch。

输入为冻结的有效 Mission、Loop identity 或待验证的 MissionEvidenceRef；输出为每个 outcome、非空边界分组
及完成检查的类型化证据条目，或精确的 revision/section/hash 不一致错误。具体工作流为使用 Mission 投影中
已计算的分区哈希编制目录，再按完整引用解析，不把 Mission 内容伪装为 Context 消息或完成证据。
示例：`entry = FrozenMissionSectionCatalog.from_mission("loop-1", mission).resolve(ref)`。
缺省 outcome/checks 不创建目录条目；空目录是有效输入，但不能用作已完成证据。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.mission_projection import EffectiveMission, EffectiveMissionProjector
from backend.app.desktop.context_curation.contracts import MissionEvidenceRef, evidence_ref_key


class MissionSectionMismatch(ValueError):
    pass


class MissionSectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: MissionEvidenceRef
    content: Any

    @model_validator(mode="after")
    def require_new_identity(self) -> MissionSectionEntry:
        if self.ref.identity_version != "mission-section-v2":
            raise ValueError("冻结 Mission section catalog 只接受新版来源身份")
        if self.ref.content_hash != EffectiveMissionProjector.section_hash(self.content):
            raise ValueError("冻结 Mission section 内容与来源哈希不一致")
        return self


class FrozenMissionSectionCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_id: str = Field(min_length=1)
    mission_revision: int = Field(gt=0)
    entries: tuple[MissionSectionEntry, ...] = ()

    @model_validator(mode="after")
    def require_frozen_identity(self) -> FrozenMissionSectionCatalog:
        keys = tuple(evidence_ref_key(item.ref) for item in self.entries)
        if len(keys) != len(set(keys)):
            raise ValueError("Mission section catalog 包含重复来源身份")
        if any(item.ref.loop_id != self.loop_id or item.ref.goal_revision != self.mission_revision for item in self.entries):
            raise ValueError("Mission section catalog 混合了 Loop 或 revision")
        if self.catalog_id != stable_expansion_hash("mission-section-catalog-v2", self.loop_id, self.mission_revision, keys):
            raise ValueError("Mission section catalog identity 与冻结内容不一致")
        return self

    @classmethod
    def from_mission(cls, loop_id: str, mission: EffectiveMission) -> FrozenMissionSectionCatalog:
        sections: list[tuple[str, str, Any]] = [("outcome", "outcome", mission.outcome)] if mission.outcome else []
        sections.extend(
            ("boundary", group, mission.boundaries[group])
            for group in ("in_scope", "required_invariants", "prohibited_actions", "legacy_text")
            if mission.boundaries.get(group)
        )
        sections.extend(("completion_check", str(item["check_id"]), item) for item in mission.completion_checks)
        entries = []
        for kind, item_id, content in sections:
            section_id = f"boundary:{item_id}" if kind == "boundary" else f"completion_check:{item_id}" if kind == "completion_check" else "outcome"
            digest = mission.section_hashes.get(section_id)
            if digest is None:
                raise MissionSectionMismatch(f"Mission section hash 不存在：{section_id}")
            entries.append(MissionSectionEntry(
                ref=MissionEvidenceRef(
                    identity_version="mission-section-v2",
                    loop_id=loop_id,
                    goal_revision=mission.revision,
                    section_kind=kind,
                    item_id=item_id,
                    content_hash=digest,
                ),
                content=content,
            ))
        keys = tuple(evidence_ref_key(item.ref) for item in entries)
        return cls(
            catalog_id=stable_expansion_hash("mission-section-catalog-v2", loop_id, mission.revision, keys),
            loop_id=loop_id,
            mission_revision=mission.revision,
            entries=tuple(entries),
        )

    def descriptors(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "section_kind": item.ref.section_kind,
                "item_id": item.ref.item_id,
                "content_hash": item.ref.content_hash,
                "descriptor": str(item.content)[:4000],
                "descriptor_truncated": len(str(item.content)) > 4000,
            }
            for item in self.entries
        )

    def resolve(self, ref: MissionEvidenceRef) -> MissionSectionEntry:
        item = next((entry for entry in self.entries if evidence_ref_key(entry.ref) == evidence_ref_key(ref)), None)
        if item is not None:
            return item
        if ref.loop_id != self.loop_id:
            raise MissionSectionMismatch("Mission Loop identity 不匹配")
        if ref.goal_revision != self.mission_revision:
            raise MissionSectionMismatch("Mission revision 已过期")
        section = next((entry for entry in self.entries if entry.ref.section_kind == ref.section_kind and entry.ref.item_id == ref.item_id), None)
        if section is None:
            raise MissionSectionMismatch("Mission section identity 不存在")
        raise MissionSectionMismatch("Mission section content hash 不匹配")
