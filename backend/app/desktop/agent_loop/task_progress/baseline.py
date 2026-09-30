"""本文件对外提供 legacy_baseline 的保守初始记忆转换。

输入为明确边界内已提交领域来源和 Mission；输出为迁移基线，绝不伪造旧 Round 版本或任务完成。
具体工作流为保留目标义务，记录可证明测试/产物/Workspace 结果；Run 自述标为待验证，历史 completeness 明确未知。
示例：legacy_baseline(mission, revision, manifest) 在独立吸收 ledger 中登记本次基线来源。
"""

from backend.app.desktop.agent_loop.task_progress.consolidation import initial_progress
from backend.app.desktop.agent_loop.task_progress.contracts import (
    TaskDeltaManifest,
    TaskItem,
    TaskProgressDocument,
)


def legacy_baseline(
    mission: dict, revision: int, manifest: TaskDeltaManifest
) -> TaskProgressDocument:
    if not manifest.complete:
        raise ValueError(manifest.blocker)
    base = initial_progress(mission, revision, migration=True)
    items = list(base.items)
    for source in manifest.sources:
        if source.kind == "user_revision":
            continue
        label = {
            "run_outcome": "历史 Run 已结算，任务结论待验证",
            "test": source.payload.get("summary") or "历史测试结果已提交",
            "workspace": "历史 Workspace 领域结果已提交",
            "artifact": f"已有执行产物：{source.payload.get('path') or '待核验'}",
        }[source.kind]
        items.append(
            TaskItem(
                item_id=f"baseline:{source.source_key}",
                description=label,
                context_ids=(source.context_id,) if source.context_id else (),
                state="blocked"
                if source.kind == "test" and source.payload.get("status") == "failed"
                else "unknown",
                support="asserted" if source.kind == "run_outcome" else "supported",
                evidence_keys=(source.source_key,),
            )
        )
    return base.model_copy(update={"items": tuple(items)})
