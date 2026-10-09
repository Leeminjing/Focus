"""本文件对外提供 UserInputApplication 的 Kernel 内用户输入处置端口。

输入为当前 Loop/Round、精确冻结用户输入及有引文的分区变更；输出为同事务处置与用户来源 Mission revision。
具体工作流为重新验证来源、类型、原文和替代对象，保留未修改分区，追加既有 Mission 与审计，
延后输入保留后继资格；不生成 Run，不允许模型发明用户目标。示例：await application.apply(session, loop, round, action)。
每次实际处置以同一输入 identity 的不可变 DomainResult 版本记录，与 Mission/intent/lifecycle 同事务；Raw accepted 与 decision 效力分开。
"""

from copy import deepcopy

from sqlalchemy import select
from focus.history import content_hash
from backend.app.desktop.agent_loop.models import AgentLoop, LoopObservation, LoopUserIntent
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.mission_service import MissionRevisionService
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest


class UserInputApplication:
    async def apply(self, session, loop, round_row, action):
        if loop.interaction_mode != "workspace_patrol":
            raise ValueError("用户输入处置只适用于工作区 Patrol")
        observation, uses, rows = await self._frozen_inputs(session, loop, round_row, action)
        await self._revise_mission(session, loop, round_row, action, uses, rows)
        for identity, use in uses.items():
            row = rows[identity]
            from backend.app.desktop.domain_evidence.repository import DomainResultRepository

            await DomainResultRepository().record(
                session,
                kind="user_revision",
                source_id=identity,
                loop_id=loop.loop_id,
                context_id=row.target_context_id,
                payload={
                    "intent_kind": "workspace_input",
                    "input_type": row.request_payload["request"]["input_type"],
                    "instruction": row.content,
                    "disposition": use.disposition,
                    "explanation": use.explanation,
                    "mission_revision": loop.goal_revision,
                },
            )
            if use.disposition == "deferred":
                row.status = "pending"
            else:
                row.status = "addressed"
                await InterventionLifecycleRepository().transition(
                    session, identity, "addressed", reason=use.explanation
                )
        for request_id in action.resolved_request_ids:
            request = await session.get(LoopWaitRequest, request_id, with_for_update=True)
            if (
                request is None
                or request.loop_id != loop.loop_id
                or request.kind != "clarification"
                or request.status != "open"
                or not rows
            ):
                raise ValueError("补充请求不能由无关输入解决")
            requests = {
                item["request_id"]: item
                for item in observation.envelope.get("information_requests", ())
            }
            if request_id not in requests or requests[request_id]["revision"] != request.revision:
                raise ValueError("补充请求不属于当前冻结版本")
            from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestService

            await LoopWaitRequestService().finish_information(session, request, tuple(rows))
        return {"input_ids": list(uses), "mission_revision": loop.goal_revision}

    @staticmethod
    async def _frozen_inputs(session, loop, round_row, action):
        observation = await session.scalar(
            select(LoopObservation).where(LoopObservation.round_id == round_row.round_id)
        )
        frozen = (
            {item["intent_id"]: item for item in observation.envelope.get("user_intents", ())}
            if observation
            else {}
        )
        uses = {item.intent_id: item for item in action.inputs}
        if len(uses) != len(action.inputs):
            raise ValueError("输入处置身份重复")
        rows = {}
        for identity in uses:
            row = await session.get(LoopUserIntent, identity, with_for_update=True)
            owner = await session.get(AgentLoop, row.loop_id) if row else None
            if (
                row is None
                or identity not in frozen
                or owner is None
                or owner.workspace_id != loop.workspace_id
                or owner.interaction_mode != "workspace_patrol"
                or row.intent_kind != "workspace_input"
                or row.status != "observed"
                or row.observed_round_id != round_row.round_id
                or row.request_payload.get("request_hash") != frozen[identity].get("request_hash")
                or row.request_payload.get("request_hash")
                != content_hash([owner.workspace_id, row.request_payload.get("request")])
                or row.content != frozen[identity].get("content")
                or row.content != row.request_payload.get("request", {}).get("content")
            ):
                raise ValueError("输入处置来源不属于当前冻结集合")
            rows[identity] = row
        return observation, uses, rows

    async def _revise_mission(self, session, loop, round_row, action, uses, rows):
        if not action.changes:
            return
        mission = await session.scalar(
            select(LoopMissionRevision).where(
                LoopMissionRevision.loop_id == loop.loop_id,
                LoopMissionRevision.revision == loop.goal_revision,
            )
        )
        if mission is None:
            raise ValueError("当前 Mission 不存在")
        sources = self._sources(mission)
        for change in action.changes:
            row = rows.get(change.intent_id)
            if (
                row is None
                or uses[change.intent_id].disposition != "decision"
                or change.quote not in row.content
            ):
                raise ValueError("Mission 修改必须引用已冻结决策原文")
            input_type = row.request_payload["request"]["input_type"]
            expected = {
                "outcome": "outcome",
                "boundary": "boundary",
                "completion_check": "completion_check",
            }
            if input_type != "information" and input_type != expected[change.section]:
                raise ValueError("特殊输入不能修改其它 Mission 分区")
            key = (
                "boundary:" + change.boundary_group
                if change.section == "boundary"
                else change.section
            )
            entries = sources.setdefault(key, [])
            ids = {entry["entry_id"] for entry in entries}
            if (
                len(set(change.supersedes)) != len(change.supersedes)
                or not set(change.supersedes).issubset(ids)
                or (change.operation == "supplement" and change.supersedes)
                or (change.operation != "supplement" and not change.supersedes)
            ):
                raise ValueError("Mission 替代必须引用当前同分区来源")
            entries[:] = [entry for entry in entries if entry["entry_id"] not in change.supersedes]
            if change.operation != "remove":
                entry_id = content_hash([change.intent_id, key, change.quote])[:32]
                if entry_id not in {entry["entry_id"] for entry in entries}:
                    entry = {
                        "entry_id": entry_id,
                        "intent_id": change.intent_id,
                        "content": change.quote,
                    }
                    if change.section == "completion_check":
                        entry["check"] = {
                            "check_id": "input-" + entry_id[:16],
                            "claim": change.quote,
                            "expected_evidence_kinds": list(change.evidence_kinds),
                            "user_verification": not bool(change.evidence_kinds),
                        }
                    entries.append(entry)
        if action.changes:
            contract = LoopMissionContract(
                outcome="\n".join(item["content"] for item in sources.get("outcome", ())) or None,
                boundaries={
                    **mission.boundaries,
                    **{
                        group: [item["content"] for item in sources.get("boundary:" + group, ())]
                        for group in ("in_scope", "required_invariants", "prohibited_actions")
                    },
                },
                completion_checks=tuple(
                    item["check"] for item in sources.get("completion_check", ())
                ),
            )
            if contract.model_dump(mode="json") != {
                "outcome": mission.outcome,
                "boundaries": mission.boundaries,
                "completion_checks": mission.completion_checks,
            }:
                loop.goal_revision += 1
                round_row.goal_revision = loop.goal_revision
                await MissionRevisionService().record(
                    session,
                    loop_id=loop.loop_id,
                    revision=loop.goal_revision,
                    contract=contract,
                    authored_by="user",
                    input_sources=sources,
                )

    @staticmethod
    def _sources(mission):
        result = deepcopy(mission.input_sources or {})
        for key, values in [
            ("outcome", [mission.outcome] if mission.outcome else []),
            *(
                ("boundary:" + group, mission.boundaries.get(group, []))
                for group in ("in_scope", "required_invariants", "prohibited_actions")
            ),
        ]:
            if key not in result:
                result[key] = [
                    {"entry_id": "legacy-" + content_hash([key, value])[:16], "content": value}
                    for value in values
                ]
        if "completion_check" not in result:
            result["completion_check"] = [
                {"entry_id": check["check_id"], "content": check["claim"], "check": check}
                for check in mission.completion_checks
            ]
        return result
