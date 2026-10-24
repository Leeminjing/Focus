"""本文件对外提供 ObservationSnapshotView 的输入前证据白名单投影。

输入为不可变 Observation envelope 与当前读取权限；输出为服务器/客户端边界摘要及可分页的轮次、后果、事实和权限条目。
具体工作流为优先读取该输入自己的 pre_input_state，验证其哈希，再按固定字段投影；绝不以当前领域表补齐历史。
示例：ObservationSnapshotView.rows(envelope, frozenset({"view_evidence"})) 返回当时已存在的 Fact revision。
"""

from backend.app.desktop.patrol.agent_loop.live.live_access import LoopLiveRedactionPolicy
from backend.app.desktop.runs.evidence.identity import canonical_hash
from fastapi import HTTPException


class ObservationSnapshotView:
    @staticmethod
    def _pick(value, keys):
        return {key: value[key] for key in keys if key in value}

    @staticmethod
    def frozen(envelope):
        state = envelope.get("pre_input_state")
        if state is None:
            return envelope
        if canonical_hash(state) != envelope.get("pre_input_hash"):
            raise HTTPException(409, "输入前快照哈希不一致")
        return state

    @classmethod
    def summary(cls, envelope):
        state = cls.frozen(envelope)
        intervention = state.get("intervention") or {}
        visible = intervention.get("visible_context") or {}
        projection = intervention.get("projection") or {}
        return {
            "snapshot_hash": envelope.get("pre_input_hash"),
            "server_sequence": intervention.get("server_sequence"),
            "live_sequence": visible.get("live_sequence"),
            "client_context_known": bool(visible) and visible.get("status") != "unknown",
            "projection_cursor": projection.get("cursor"),
            "projection_lag": projection.get("lag"),
            "projection_failures": len(projection.get("failures") or []),
            "round_count": len(intervention.get("rounds") or []),
            "fact_count": len(intervention.get("facts") or []),
        }

    @classmethod
    def rows(cls, envelope, permissions):
        state = cls.frozen(envelope)
        intervention = state.get("intervention") or {}
        rows = []
        identity = ("round_id", "observation_id", "generation", "observation_generation", "decision_id", "context_id", "target_context_id", "run_id", "directive_id", "worker_request_id", "dispatch_id", "portfolio_revision_id", "adoption_id", "status", "lifecycle_state", "settled_at", "completed_at", "number", "permissions", "access_mode", "workspace_slot_id", "authority_committed_at")
        for group in ("rounds", "runs", "directives", "workers", "dispatches", "publications", "adoptions"):
            for item in intervention.get(group, []):
                row = {"kind": group, **cls._pick(item, identity)}
                if item.get("authority"):
                    row["authority"] = cls._pick(item["authority"], ("loop_id", "round_id", "decision_id", "context_id", "grant_revision", "access_mode", "source"))
                if item.get("decision"):
                    row["decision"] = cls._pick(item["decision"], ("decision_id", "status", "authority_committed_at"))
                rows.append(row)
        mission = state.get("mission") or state.get("goal")
        if mission:
            rows.append({"kind": "mission", **cls._pick(mission, ("mission_revision_id", "revision", "outcome", "boundaries", "completion_checks"))})
        for fact in intervention.get("facts", []):
            item = {"kind": "fact", **cls._pick(fact, ("fact_id", "revision", "state", "fact_type", "source_context_id", "source_run_id"))}
            if "view_evidence" in permissions:
                item["evidence"] = cls._pick(fact, ("presentation", "evidence", "observer", "verifier"))
            rows.append(item)
        projection = intervention.get("projection") or {}
        rows.extend({"kind": "projection_failure", **cls._pick(item, ("failure_id", "unit_kind", "unit_id", "status", "boundary_sequence", "error_class"))} for item in projection.get("failures", []))
        grant = state.get("grant") or {}
        rows.append({"kind": "permission_ceiling", "scope": "grant", "equipment": cls._pick(grant.get("equipment") or {}, ("permissions", "access_mode"))})
        for context in state.get("portfolio_frontier", []):
            rows.append({"kind": "permission_ceiling", "scope": "context", **cls._pick(context, ("context_id", "role")), "equipment": cls._pick(context.get("equipment") or {}, ("permissions", "access_mode")), "lane_policy": cls._pick(context.get("lane_policy") or {}, ("workspace_mode",))})
        visible = intervention.get("visible_context") or {}
        rows.extend({"kind": "visible_fact", **cls._pick(item, ("fact_id", "revision"))} for item in visible.get("fact_refs", []))
        for preview in visible.get("run_previews", []):
            item = {"kind": "visible_run", **cls._pick(preview, ("run_id", "context_id", "message_id", "sequence", "role", "truncated", "gap"))}
            if "view_evidence" in permissions:
                item["evidence"] = {"text": preview.get("text")}
            rows.append(item)
        return LoopLiveRedactionPolicy.redact_value(rows, permissions)
