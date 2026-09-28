"""本文件对外提供三档文件模式、真实路径解释及文件准入的测试用例。

输入为真实宿主路径、访问策略和受治理上下文；输出为规范化路径与允许、待审或拒绝结论。
具体工作流为先核对 Windows 路径别名，再验证只读、工作区可写、完全访问和旧值迁移。
示例：运行 python -m pytest backend/tests/test_access_policy.py。
"""

import os
from pathlib import Path

import pytest

from focus.security import (
    AccessDecision,
    AccessMode,
    AccessOperation,
    AccessPolicy,
    canonical_target,
    canonical_text,
    decide_path_access,
    is_within,
    policy_from_context,
)


def test_canonical_target_resolves_relative_against_workspace(tmp_path):
    assert canonical_target(tmp_path, "src/a.py") == (tmp_path / "src" / "a.py").resolve()


def test_canonical_target_keeps_absolute_path(tmp_path):
    target = tmp_path / "a.py"
    assert canonical_target(tmp_path, str(target)) == target.resolve()


def test_canonical_target_expands_escape_outside_workspace(tmp_path):
    escaped = canonical_target(tmp_path, "../outside.txt")
    assert not is_within(tmp_path, escaped)
    assert escaped == (tmp_path.parent / "outside.txt").resolve()


def test_is_within_root_itself_and_descendant(tmp_path):
    assert is_within(tmp_path, tmp_path)
    assert is_within(tmp_path, tmp_path / "deep" / "a.txt")


def test_is_within_rejects_sibling_sharing_prefix(tmp_path):
    sibling = Path(str(tmp_path) + "-sibling")
    assert not is_within(tmp_path, sibling)


def test_is_within_rejects_unrelated_root(tmp_path):
    assert not is_within(tmp_path, tmp_path.parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path prefix")
def test_canonical_text_unifies_extended_prefix(tmp_path):
    plain = tmp_path / "a.txt"
    assert canonical_text(Path("\\\\?\\" + str(plain))) == canonical_text(plain)


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC form")
def test_canonical_text_unifies_extended_unc_prefix():
    extended = Path("\\\\?\\UNC\\server\\share\\a.txt")
    plain = Path("\\\\server\\share\\a.txt")
    assert canonical_text(extended) == canonical_text(plain)


def _workspace_policy(tmp_path) -> AccessPolicy:
    return AccessPolicy(mode=AccessMode.WORKSPACE_WRITE, workspace=tmp_path, roots=(tmp_path,))


def test_workspace_mode_allows_inside_root_for_both_operations(tmp_path):
    policy = _workspace_policy(tmp_path)
    inside = tmp_path / "a.txt"
    assert decide_path_access(policy, inside, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, inside, AccessOperation.WRITE) is AccessDecision.ALLOW


def test_workspace_mode_allows_read_and_denies_write_outside_root(tmp_path):
    policy = _workspace_policy(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.DENY


def test_full_mode_allows_outside_root(tmp_path):
    policy = AccessPolicy(mode=AccessMode.FULL, workspace=tmp_path, roots=(tmp_path,))
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.ALLOW


def test_identity_without_roots_denies_writes(tmp_path):
    policy = AccessPolicy(mode=AccessMode.WORKSPACE, workspace=tmp_path, roots=())
    inside = tmp_path / "a.txt"
    assert decide_path_access(policy, inside, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, inside, AccessOperation.WRITE) is AccessDecision.DENY


def test_decision_can_deny_outside_writes(tmp_path):
    assert set(AccessDecision) == {AccessDecision.ALLOW, AccessDecision.ASK, AccessDecision.DENY}
    policy = _workspace_policy(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.DENY


def test_policy_from_context_defaults_to_read_only(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path)})
    assert policy.mode is AccessMode.READ_ONLY
    assert policy.workspace == tmp_path.resolve()
    assert tmp_path.resolve() in policy.roots


def test_policy_from_context_reads_declared_mode(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path), "access_mode": "full"})
    assert policy.mode is AccessMode.DANGER_FULL_ACCESS


def test_policy_from_context_treats_unknown_mode_as_read_only(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path), "access_mode": "bogus"})
    assert policy.mode is AccessMode.READ_ONLY


def test_read_only_denies_workspace_write_and_allows_external_read(tmp_path):
    policy = AccessPolicy(mode=AccessMode.READ_ONLY, workspace=tmp_path, roots=(tmp_path,))
    assert decide_path_access(policy, tmp_path / "a", AccessOperation.WRITE) is AccessDecision.DENY
    assert decide_path_access(policy, tmp_path.parent / "outside", AccessOperation.READ) is AccessDecision.ALLOW


def test_old_mode_values_migrate_explicitly():
    assert AccessMode("workspace") is AccessMode.WORKSPACE_WRITE
    assert AccessMode("full") is AccessMode.DANGER_FULL_ACCESS


def test_policy_from_context_requires_workspace():
    with pytest.raises(RuntimeError, match="workspace"):
        policy_from_context({})


def test_policy_from_context_adds_global_home_when_allowed(tmp_path):
    from focus.config.layered import global_home

    policy = policy_from_context({"workspace": str(tmp_path), "allow_global_config": True})
    assert policy.roots[0] == tmp_path.resolve()
    assert global_home().resolve() in policy.roots


def test_policy_from_context_keeps_workspace_only_by_default(tmp_path):
    from focus.config.layered import global_home

    policy = policy_from_context({"workspace": str(tmp_path)})
    assert global_home().resolve() not in policy.roots
