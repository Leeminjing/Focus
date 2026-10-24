"""把安全的索引阶段／数量写入现有活动流；旧 Observation 的进度不覆盖当前轮。"""

import asyncio
import uuid
from time import monotonic

from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopRound


class IndexProgressReporter:
    def __init__(self, sessions, observation, *, fencing_token=None):
        self._sessions = sessions
        self._observation = observation
        self._build_id = uuid.uuid4().hex
        self._revision = 0
        self._lock = asyncio.Lock()
        self._last = {}
        self._fencing_token = fencing_token

    async def report(self, source_revision_id=None, index_phase="preparing", status="running", **counts):
        labels = {"preparing": "准备索引", "local": "局部投影与验证", "local_projection": "局部投影",
                  "local_verifier": "局部验证", "interpretation": "整体解释",
                  "interpretation_verifier": "联合引文验证", "complete": "索引准备完成"}
        label = labels.get(index_phase, "准备索引")
        if index_phase == "complete" and status != "ready":
            label = "索引准备"
        suffix = {"queued": "，有请求等待模型名额", "admitting": "，检查授权额度",
                  "blocked": "，额度不足，等待预算调整", "capacity_blocked": "，请求超出模型窗口",
                  "failed": "，准备失败",
                  "cancelled": "，已取消"}.get(status, "")
        summary = label + suffix
        if "completed_segments" in counts:
            summary += f" · 局部 {counts['completed_segments']}/{counts['total_segments']} 段"
        async with self._lock:
            key = (source_revision_id, index_phase)
            prior = self._last.get(key)
            # Model calls may change rapidly; retain phase boundaries and all terminal outcomes.
            now = monotonic()
            if prior and status == prior[0] and now - prior[1] < 0.25:
                return
            observation = self._observation
            async with self._sessions.begin() as session:
                loop = await session.get(AgentLoop, observation.loop_id, with_for_update=True)
                row = await session.get(LoopRound, observation.round_id)
                if (loop is None or loop.status != "running" or row is None
                    or row.observation_id != observation.decision_inputs_ref
                    or row.decision_id is not None
                    or row.status in {"settled", "error", "superseded"}
                    or loop.authority_revision != row.authority_revision):
                    return
                if self._fencing_token is not None:
                    from backend.app.desktop.patrol.agent_loop.governance.ownership import LoopFencingGuard, KernelFencingRejected

                    try:
                        await LoopFencingGuard().validate_current(session, row.round_id, self._fencing_token)
                    except KernelFencingRejected:
                        return
                self._revision += 1
                await LoopEventJournal().append(session, observation.loop_id, CanonicalEventDraft(
                    kind="semantic_index.progress", entity_type="loop_activity", entity_id=self._build_id,
                    entity_revision=self._revision, correlation_id=observation.round_id,
                    payload={"summary": summary, "round_id": observation.round_id,
                             "observation_id": observation.decision_inputs_ref,
                             "source_revision_id": source_revision_id, "index_phase": index_phase,
                             "status": status, **counts},
                    idempotency_key=f"index-progress:{self._build_id}:{self._revision}",
                ))
            self._last[key] = (status, now)
