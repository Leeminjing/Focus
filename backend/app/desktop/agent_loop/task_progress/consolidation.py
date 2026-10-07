"""本文件对外提供 TaskProgressConsolidator 和 initial_progress 的纯记忆转换。

输入为唯一前序、冻结领域来源、Mission 与受校验 semantic patch；输出为完整后继及 Round/Run 贡献。
具体工作流为复用 candidate_contract 统一准入，保留全部旧事项，替代项保留为不再适用，再按执行/观察归属组织贡献。
它不访问实时世界、数据库、模型或执行权威。示例：consolidator.apply(inputs, candidate, mission)。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.task_progress.candidate_contract import validate_candidate
from backend.app.desktop.agent_loop.task_progress.contracts import (
    ProgressCandidate,
    RoundContribution,
    RoundDecisionInputs,
    RunContribution,
    TaskItem,
    TaskProgressDocument,
)


def initial_progress(
    mission: dict, revision: int, *, migration: bool = False
) -> TaskProgressDocument:
    items = [
        TaskItem(
            item_id=f"mission:{revision}:outcome",
            description=str(mission.get("outcome") or "任务目标待确认"),
            state="not_started",
        )
    ]
    for check in mission.get("completion_checks") or ():
        items.append(
            TaskItem(
                item_id=f"check:{check['check_id']}",
                description=check["claim"],
                state="not_started",
            )
        )
    return TaskProgressDocument(
        mission_revision=revision,
        items=tuple(items),
        baseline_kind="migration" if migration else "initial",
        history_complete=not migration,
    )


class TaskProgressConsolidator:
    def apply(
        self, inputs: RoundDecisionInputs, candidate: ProgressCandidate, mission: dict
    ) -> tuple[TaskProgressDocument, RoundContribution]:
        candidate = validate_candidate(inputs, candidate)
        sources = {item.source_key: item for item in inputs.task_delta.sources}
        items = {item.item_id: item for item in inputs.previous_progress.items}
        for change in candidate.changes:
            for item_id in change.supersedes:
                items[item_id] = items[item_id].model_copy(
                    update={"state": "not_applicable"}
                )
            items[change.item_id] = change
        for assessment in candidate.source_assessments:
            if assessment.disposition == "unknown":
                source = sources[assessment.source_key]
                item_id = f"source:{assessment.source_key}"
                items.setdefault(
                    item_id,
                    TaskItem(
                        item_id=item_id,
                        description=assessment.explanation,
                        context_ids=(source.context_id,) if source.context_id else (),
                        state="unknown",
                        evidence_keys=(source.source_key,),
                    ),
                )
        revision = int(
            mission.get("revision") or inputs.previous_progress.mission_revision
        )
        self._mission_items(
            items, mission, revision, inputs.previous_progress.mission_revision
        )
        document = TaskProgressDocument(
            mission_revision=revision,
            items=tuple(items.values()),
            baseline_kind="round",
            history_complete=inputs.previous_progress.history_complete,
        )
        by_run: dict[str, list] = {}
        direct = []
        for source in inputs.task_delta.sources:
            if source.run_id:
                by_run.setdefault(source.run_id, []).append(source)
            else:
                direct.append(source.source_key)
        runs = tuple(
            RunContribution(
                run_id=run_id,
                execution_round_id=group[0].execution_round_id,
                observed_round_id=inputs.round_id,
                source_assessments=tuple(
                    assessment
                    for assessment in candidate.source_assessments
                    if assessment.source_key in {source.source_key for source in group}
                ),
                source_keys=tuple(item.source_key for item in group),
                changes=tuple(
                    change
                    for change in candidate.changes
                    if set(change.evidence_keys).intersection(
                        item.source_key for item in group
                    )
                ),
            )
            for run_id, group in by_run.items()
        )
        return document, RoundContribution(
            round_id=inputs.round_id,
            run_contributions=runs,
            direct_source_keys=tuple(direct),
            changes=candidate.changes,
            source_assessments=candidate.source_assessments,
        )

    @staticmethod
    def _mission_items(
        items: dict[str, TaskItem], mission: dict, revision: int, previous_revision: int
    ) -> None:
        checks = {
            f"check:{item['check_id']}": item
            for item in mission.get("completion_checks") or ()
        }
        for item_id, check in checks.items():
            old = items.get(item_id)
            if (
                old is None
                or old.description != check["claim"]
                or revision != previous_revision
            ):
                items[item_id] = TaskItem(
                    item_id=item_id,
                    description=check["claim"],
                    state="unknown" if old else "not_started",
                )
        if revision != previous_revision:
            for item_id, old in tuple(items.items()):
                if item_id.startswith("check:") and item_id not in checks:
                    items[item_id] = old.model_copy(update={"state": "not_applicable"})
                if (
                    item_id.startswith("mission:")
                    and item_id != f"mission:{revision}:outcome"
                ):
                    items[item_id] = old.model_copy(update={"state": "not_applicable"})
            item_id = f"mission:{revision}:outcome"
            items.setdefault(
                item_id,
                TaskItem(
                    item_id=item_id,
                    description=str(mission.get("outcome") or "任务目标待确认"),
                    state="not_started",
                ),
            )
