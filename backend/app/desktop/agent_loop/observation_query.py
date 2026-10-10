"""本文件对外提供 ObservationInspectionQuery 的冻结输入只读检查。

输入为 Desktop 会话、Loop/Observation 身份、section 与绑定内容哈希的游标；输出为安全摘要或有界冻结条目。
具体工作流为复用当前 Live 权限，核对不可变输入身份，白名单投影并分页；计数仅公开非负整数，异常指标降级 unknown，不读取当前 head 回填历史，不写领域状态。
示例：await ObservationInspectionQuery().read(session, "loop", "observation", section="sources", limit=40)。
"""

from __future__ import annotations

import base64
import json
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.models import LoopObservation, LoopRound
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionInputs
from backend.app.desktop.domain_evidence.identity import canonical_hash


ObservationSection = Literal["summary", "previous_progress", "sources", "lineage"]


class ObservationInspectionQuery:
    async def read(
        self, session: AsyncSession, loop_id: str, observation_id: str, *,
        section: ObservationSection = "summary", cursor: str | None = None, limit: int = 40,
    ) -> dict:
        access = await LoopLiveAccessPolicy().resolve(session, loop_id)
        observation = await session.get(LoopObservation, observation_id)
        if observation is None or observation.loop_id != loop_id:
            raise HTTPException(404, "Observation 不存在")
        frozen = await session.get(LoopDecisionInputs, observation_id)
        payload = frozen.payload if frozen is not None else {}
        if frozen is not None and (
            frozen.loop_id != loop_id or frozen.round_id != observation.round_id
            or payload.get("observation_hash") != observation.envelope_hash
            or payload.get("round_id") != observation.round_id
            or canonical_hash(payload) != frozen.content_hash
        ):
            raise HTTPException(409, "冻结输入身份不一致")
        round_row = await session.get(LoopRound, observation.round_id)
        summary = self._summary(observation, frozen, round_row, access.permissions)
        if section == "summary":
            if cursor:
                raise HTTPException(422, "摘要不接受分页游标")
            return summary
        if section not in {"previous_progress", "sources", "lineage"} or not 1 <= limit <= 120:
            raise HTTPException(422, "无效的冻结读取范围")
        binding = [loop_id, observation_id, observation.envelope_hash, frozen.content_hash if frozen else None, section]
        offset = self._offset(cursor, binding)
        rows = self._rows(payload, section, access.permissions)
        if offset > len(rows):
            raise HTTPException(422, "游标超出冻结集合")
        end = min(offset + limit, len(rows))
        return {
            **summary, "section": section, "items": rows[offset:end], "total": len(rows),
            "has_more": end < len(rows),
            "next_cursor": self._cursor(binding, end) if end < len(rows) else None,
        }

    @staticmethod
    def _summary(observation, frozen, round_row, permissions) -> dict:
        payload = frozen.payload if frozen else {}
        previous = payload.get("previous_progress") or {}
        delta = payload.get("task_delta") or {}
        lineage = payload.get("lineage") or {}
        envelope = observation.envelope
        return {
            "loop_id": observation.loop_id, "observation_id": observation.observation_id,
            "round_id": observation.round_id, "round_number": round_row.number if round_row else None,
            "observation_hash": observation.envelope_hash,
            "frozen_at": observation.created_at.isoformat(),
            "inputs_hash": frozen.content_hash if frozen else None,
            "availability": "available" if frozen else "legacy",
            "evidence_visible": "view_evidence" in permissions,
            "goal_revision": envelope.get("goal_revision"),
            "authority_revision": envelope.get("authority_revision"),
            "previous_progress": {
                "progress_id": payload.get("previous_progress_id"),
                "content_hash": payload.get("previous_progress_hash"),
                "mission_revision": previous.get("mission_revision"),
                "item_count": len(previous.get("items", [])),
                "history_complete": previous.get("history_complete"),
            },
            "sources": {"total": len(delta.get("sources", [])), "complete": delta.get("complete"), "manifest_hash": payload.get("manifest_hash")},
            "lineage": {"node_count": len(lineage.get("nodes", [])), "edge_count": len(lineage.get("edges", [])), "topology_hash": payload.get("topology_hash"), "complete": lineage.get("complete")},
        }

    @classmethod
    def _rows(cls, payload, section, permissions) -> list[dict]:
        if section == "previous_progress":
            keys = ("item_id", "description", "context_ids", "state", "support", "blockers", "corrects", "supersedes")
            if "view_evidence" in permissions:
                keys += ("evidence_keys",)
            rows = [cls._pick(item, keys) for item in (payload.get("previous_progress") or {}).get("items", [])]
        elif section == "sources":
            rows = []
            for source in (payload.get("task_delta") or {}).get("sources", []):
                row = cls._pick(source, ("source_key", "kind", "source_id", "version", "context_id", "run_id", "execution_round_id"))
                if "view_evidence" in permissions:
                    body = source.get("payload") or {}
                    row["evidence"] = cls._pick(body, ("status", "summary", "title"))
                    row["evidence"] = {key: value for key, value in row["evidence"].items() if value is None or isinstance(value, (str, int, float, bool))}
                    if "metrics" in body or "count_status" in body:
                        row["evidence"]["metrics"] = cls._metrics(body.get("metrics", body))
                rows.append(row)
        else:
            lineage = payload.get("lineage") or {}
            rows = [
                {"kind": "node", **cls._pick(item, ("revision_id", "context_id", "generation", "redacted"))}
                for item in lineage.get("nodes", [])
            ] + [
                {"kind": "edge", **cls._pick(item, ("source_context_id", "source_revision_id", "target_context_id", "target_revision_id"))}
                for item in lineage.get("edges", [])
            ]
        return LoopLiveRedactionPolicy.redact_value(rows, permissions)

    @staticmethod
    def _metrics(value) -> dict:
        if not isinstance(value, dict):
            return {"count_status": "unknown"}
        status = value.get("count_status")
        if status in ("unknown", "not_applicable"):
            return {"count_status": status}
        keys = ("passed", "failed", "skipped") + (("total",) if "total" in value else ())
        if status != "exact" or any(type(value.get(key)) is not int or value[key] < 0 for key in keys):
            return {"count_status": "unknown"}
        return {"count_status": "exact", **{key: value[key] for key in keys}}

    @staticmethod
    def _pick(value, keys) -> dict:
        return {key: value[key] for key in keys if key in value}

    @staticmethod
    def _cursor(binding, offset) -> str:
        raw = json.dumps([binding, offset], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _offset(cursor, binding) -> int:
        if cursor is None:
            return 0
        try:
            identity, offset = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if identity != binding or type(offset) is not int or offset < 0:
                raise ValueError()
            return offset
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "游标不属于此冻结输入范围") from None
