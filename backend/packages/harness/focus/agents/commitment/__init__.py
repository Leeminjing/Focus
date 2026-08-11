"""
本文件对外提供 CommitmentMiddleware、TaskEnvelope、ReviewedDelegator 及承诺层测试所需的稳定导出。

输入:
    调用方从 focus.agents.commitment 导入的公开类型、核心类或兼容辅助函数。

输出:
    CommitmentMiddleware — 可装配到 lead agent 的承诺层中间件。
    TaskEnvelope — Supervisor 委派单个阶段时使用的结构化任务信封。
    ReviewedDelegator — 执行 Worker-Evaluator 审核闭环的委派器。

具体工作流:
    (1) 从各职责模块导入公开对象。
    (2) 通过 __all__ 声明稳定导出集合。
    (3) 调用方无需感知内部模块拆分即可使用承诺层。

示例:
    from focus.agents.commitment import CommitmentMiddleware, TaskEnvelope
"""

from focus.agents.commitment.artifacts import (
    _build_final_message,
    _write_contract,
    _write_knowledge,
)
from focus.agents.commitment.delegation import ReviewedDelegator
from focus.agents.commitment.middleware import (
    CommitmentMiddleware,
    _commit_instruction,
    _extract_uploads_tag,
)
from focus.agents.commitment.references import _SearchResultParser
from focus.agents.commitment.schemas import (
    CommitmentState,
    ReviewOutput,
    TaskEnvelope,
    WorkerOutput,
)
from focus.agents.commitment.stage_rules import (
    _contains_unresolved_versions,
    _context7_candidate_version,
    _context7_stable_version,
    _extract_structured,
    _filter_stage_four_result,
    _has_open_conflicts,
    _normalize_stage_three_result,
    _safe_segment,
    _stage_four_needs_review,
    _stage_timeout,
    _validate_stage_result,
)
from focus.agents.commitment.workflow import (
    _build_supervisor,
    _human_payload,
    _review_human_revision,
    build_delegate_with_review_tool,
)

__all__ = [
    "CommitmentMiddleware",
    "CommitmentState",
    "_commit_instruction",
    "ReviewOutput",
    "ReviewedDelegator",
    "TaskEnvelope",
    "WorkerOutput",
    "_SearchResultParser",
    "_build_final_message",
    "_build_supervisor",
    "_contains_unresolved_versions",
    "_context7_candidate_version",
    "_context7_stable_version",
    "_extract_structured",
    "_extract_uploads_tag",
    "_filter_stage_four_result",
    "_has_open_conflicts",
    "_human_payload",
    "_normalize_stage_three_result",
    "_review_human_revision",
    "_safe_segment",
    "_stage_four_needs_review",
    "_stage_timeout",
    "_validate_stage_result",
    "_write_contract",
    "_write_knowledge",
    "build_delegate_with_review_tool",
]
