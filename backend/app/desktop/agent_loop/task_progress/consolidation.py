"""本文件对外提供 TaskProgressConsolidator 和 initial_progress 的纯记忆转换。

输入为唯一前序、冻结领域来源、Mission 与受校验 semantic patch；输出为完整后继及 Round/Run 贡献。
具体工作流为保留全部旧事项，验证新增、corrects 与 supersedes 的来源和身份，替代项保留为不再适用，再按执行/观察归属组织贡献。
它不访问实时世界、数据库、模型或执行权威。示例：consolidator.apply(inputs, candidate, mission)。
"""

from __future__ import annotations

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
        if not inputs.task_delta.complete:
            raise ValueError(inputs.task_delta.blocker or "任务增量不完整")
        sources = {item.source_key: item for item in inputs.task_delta.sources}
        assessments = {item.source_key: item for item in candidate.source_assessments}
        referenced = {
            key for change in candidate.changes for key in change.evidence_keys
        }
        if len(assessments) != len(candidate.source_assessments) or not set(
            assessments
        ).issubset(sources):
            raise ValueError("来源解释重复或引用冻结范围之外的来源")
        if set(sources) != referenced | set(assessments):
            raise ValueError("全部冻结来源必须被解释，不能静默吸收")
        items = {item.item_id: item for item in inputs.previous_progress.items}
        previous_ids = set(items)
        changed_ids = {item.item_id for item in candidate.changes}
        if len(changed_ids) != len(candidate.changes):
            raise ValueError("同一候选重复修正事项")
        if any(
            changed_ids.intersection(change.supersedes) for change in candidate.changes
        ):
            raise ValueError("被替代事项不能在同一候选中再次修改")
        for change in candidate.changes:
            if not set(change.corrects).issubset(items):
                raise ValueError("修正必须引用已存在的任务 identity")
            if change.item_id in change.supersedes or not set(
                change.supersedes
            ).issubset(previous_ids):
                raise ValueError("替代必须引用前序其他任务 identity")
            self._validate_change(change, items.get(change.item_id), sources)
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
    def _validate_change(change: TaskItem, old: TaskItem | None, sources: dict) -> None:
        if not change.evidence_keys or not set(change.evidence_keys).issubset(sources):
            raise ValueError("任务变化必须仅引用本轮冻结领域来源")
        if old is not None and old != change and old.item_id not in change.corrects:
            raise ValueError("修改旧事项必须显式 corrects 原 identity")
        if (
            old is not None
            and old.blockers
            and not set(old.blockers).issubset(change.blockers)
            and not change.corrects
        ):
            raise ValueError("解除 blocker 必须显式关联原事项")
        evidence = [sources[key] for key in change.evidence_keys]
        if change.support == "supported" and not any(
            item.kind in {"test", "workspace", "artifact"} for item in evidence
        ):
            raise ValueError("Agent 自述不能单独构成 supported")
        if change.state == "completed" and any(
            item.kind == "test" and item.payload.get("status") != "verified"
            for item in evidence
        ):
            raise ValueError("失败或未知测试不能支持完成")
        if (
            old is not None
            and old.state == "conflicted"
            and change.state == "completed"
            and not change.corrects
        ):
            raise ValueError("冲突不能静默覆盖为完成")

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
