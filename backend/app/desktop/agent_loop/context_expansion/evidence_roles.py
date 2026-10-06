"""本文件对外提供semantic_unit_roles、context_evidence_role、structured_evidence_role与evidence_role_contract。
输入为冻结semantic unit kind、来源Context职责或类型化结构证据，可指定本次可引用的kind、职责与Run来源可见性；输出为既有证据角色及同一政策的JSON兼容只读投影。
具体工作流为复用固定分类映射、来源职责别名与Run终态分类，向规划输入投影政策并由Corpus按同一规则形成最终证据角色。
投影每次返回独立容器，不授予执行权、不改写引用或增加重试；示例：semantic_unit_roles("claim")返回("conversation",)。
"""

from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import EvidenceRole, SemanticUnitKind
from backend.app.desktop.context_curation import MaterialEvidenceRef, RunResultEvidenceRef, WorkspaceEffectEvidenceRef


_UNIT_ROLES: dict[SemanticUnitKind, tuple[EvidenceRole, ...]] = {
    "decision": ("decision",),
    "claim": ("conversation",),
    "hypothesis": ("conversation",),
    "unresolved_question": ("conversation",),
    "implementation_effect": ("implementation", "workspace_effect"),
    "verification_result": ("test",),
    "failure": ("failure",),
}
_CONTEXT_ROLES: dict[str, EvidenceRole] = {
    "implementation": "implementation", "testing": "test", "test": "test", "verification": "test",
    "requirements": "requirement", "requirement": "requirement", "failure": "failure", "decision": "decision",
}
_FAILURE_STATUSES = ("error", "failed", "failure")
_STRUCTURED_ROLES: dict[str, EvidenceRole] = {"mission": "requirement", "material": "material", "workspace_effect": "workspace_effect"}


def semantic_unit_roles(kind: SemanticUnitKind) -> tuple[EvidenceRole, ...]:
    return _UNIT_ROLES[kind]


def context_evidence_role(role: str | None) -> EvidenceRole | None:
    return _CONTEXT_ROLES.get((role or "").casefold())


def structured_evidence_role(ref, content: Any) -> EvidenceRole:
    if isinstance(ref, RunResultEvidenceRef):
        status = str(content.get("status") if isinstance(content, dict) else "").casefold()
        return "failure" if status in _FAILURE_STATUSES else "test"
    if isinstance(ref, WorkspaceEffectEvidenceRef):
        return _STRUCTURED_ROLES["workspace_effect"]
    if isinstance(ref, MaterialEvidenceRef):
        return _STRUCTURED_ROLES["material"]
    return _STRUCTURED_ROLES["mission"]


def evidence_role_contract(
    *, unit_kinds: tuple[SemanticUnitKind, ...] | None = None, context_roles: tuple[str, ...] | None = None,
    include_run_results: bool = True,
) -> dict[str, Any]:
    contract = {
        "semantic_unit_kinds": {kind: list(roles) for kind, roles in _UNIT_ROLES.items() if unit_kinds is None or kind in unit_kinds},
        "source_context_roles": {role: value for role, value in _CONTEXT_ROLES.items() if context_roles is None or role in {item.casefold() for item in context_roles}},
        "structured_sources": dict(_STRUCTURED_ROLES),
        "run_result": {"failure_statuses": list(_FAILURE_STATUSES), "failure_role": "failure", "other_role": "test"},
        "message_fallback_role": "conversation",
    }
    if not include_run_results:
        del contract["run_result"]
    return contract
