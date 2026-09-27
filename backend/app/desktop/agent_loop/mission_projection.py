r"""本文件对外提供 EffectiveMission 与 EffectiveMissionProjector，统一结构化和旧版 Mission 的只读投影。

输入为当前 LoopMissionRevision、LoopGoalRevision 或历史 observation；输出为带 revision、分区内容哈希与稳定检查标识的
EffectiveMission 或单一模型可见 Mission。具体工作流为优先读取结构化 revision，旧记录经 LegacyMissionAdapter 无损保留原始字段，
再按分区生成确定性哈希；model_payload 与 observation_payload 不暴露兼容来源的重复目标。
示例：`view = EffectiveMissionProjector.from_rows(structured=mission, legacy=None)`。
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter, LoopMissionContract
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import LoopGoalRevision


class EffectiveMission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(gt=0)
    source_format: Literal["structured", "legacy_adapter"]
    outcome: str
    boundaries: dict[str, Any]
    completion_checks: tuple[dict[str, Any], ...]
    section_hashes: dict[str, str]
    legacy_source: dict[str, Any] | None = None

    def model_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"legacy_source"})


class EffectiveMissionProjector:
    @staticmethod
    def section_hash(content: Any) -> str:
        return sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def observation_payload(cls, observation: Any) -> dict[str, Any]:
        payload = observation.model_dump(mode="json")
        mission = payload.pop("mission", None)
        legacy = payload.pop("goal", None)
        if mission is None and legacy is not None:
            contract = LegacyMissionAdapter.convert(**legacy)
            mission = cls._project(contract, observation.goal_revision, "legacy_adapter", legacy).model_payload()
        payload["effective_mission"] = mission
        return payload

    @classmethod
    def from_observation(cls, observation: Any) -> EffectiveMission:
        mission = getattr(observation, "mission", None)
        if mission is not None:
            if "section_hashes" in mission and "source_format" in mission:
                return EffectiveMission.model_validate(mission)
            contract = LoopMissionContract.model_validate(mission)
            return cls._project(contract, observation.goal_revision, "structured", None)
        legacy = getattr(observation, "goal", None)
        if legacy is None:
            raise ValueError("冻结 observation 缺少 Mission")
        contract = LegacyMissionAdapter.convert(**legacy)
        return cls._project(contract, observation.goal_revision, "legacy_adapter", legacy)

    @classmethod
    def from_rows(
        cls,
        *,
        structured: LoopMissionRevision | None,
        legacy: LoopGoalRevision | None,
    ) -> EffectiveMission:
        if structured is not None:
            contract = LoopMissionContract.model_validate({
                "outcome": structured.outcome,
                "boundaries": structured.boundaries,
                "completion_checks": structured.completion_checks,
            })
            return cls._project(contract, structured.revision, "structured", None)
        if legacy is None:
            raise ValueError("当前 Loop 缺少 Mission revision")
        source = {
            "goal": legacy.goal,
            "task_contract": legacy.task_contract,
            "acceptance_criteria": legacy.acceptance_criteria,
        }
        contract = LegacyMissionAdapter.convert(**source)
        return cls._project(contract, legacy.revision, "legacy_adapter", source)

    @staticmethod
    def _project(
        contract: LoopMissionContract,
        revision: int,
        source_format: Literal["structured", "legacy_adapter"],
        legacy_source: dict[str, Any] | None,
    ) -> EffectiveMission:
        boundaries = contract.boundaries.model_dump(mode="json")
        checks = tuple(item.model_dump(mode="json") for item in contract.completion_checks)
        sections: dict[str, Any] = {"outcome": contract.outcome}
        for group in ("in_scope", "required_invariants", "prohibited_actions", "legacy_text"):
            if boundaries.get(group):
                sections[f"boundary:{group}"] = boundaries[group]
        for item in checks:
            sections[f"completion_check:{item['check_id']}"] = item
        hashes = {key: EffectiveMissionProjector.section_hash(value) for key, value in sections.items()}
        return EffectiveMission(
            revision=revision,
            source_format=source_format,
            outcome=contract.outcome,
            boundaries=boundaries,
            completion_checks=checks,
            section_hashes=hashes,
            legacy_source=legacy_source,
        )
