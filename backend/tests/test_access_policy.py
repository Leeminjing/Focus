"""本地访问策略的路径解释与准入判定用例。

输入为真实宿主路径、访问策略与上下文；输出为规范化结果、归属判定与准入结论。
工作流先逐例锁定路径解释行为（含 Windows 扩展路径前缀与 UNC 写法），再锁定
「工作根内/外 × 读/写」四种准入组合与策略构造的失败语义。
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
    return AccessPolicy(mode=AccessMode.WORKSPACE, workspace=tmp_path, roots=(tmp_path,))


def test_workspace_mode_allows_inside_root_for_both_operations(tmp_path):
    policy = _workspace_policy(tmp_path)
    inside = tmp_path / "a.txt"
    assert decide_path_access(policy, inside, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, inside, AccessOperation.WRITE) is AccessDecision.ALLOW


def test_workspace_mode_asks_outside_root_for_both_operations(tmp_path):
    policy = _workspace_policy(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.READ) is AccessDecision.ASK
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.ASK


def test_full_mode_allows_outside_root(tmp_path):
    policy = AccessPolicy(mode=AccessMode.FULL, workspace=tmp_path, roots=(tmp_path,))
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.ALLOW


def test_identity_without_roots_asks_for_every_target(tmp_path):
    """没有被授予任何工作根的执行身份：所有受治理目标都在工作根之外，逐次请求批准。

    正常派生路径（`derive_security_context`）保证工作根恒含工作区，因此这个形状不会由派生产生；
    本用例锁定的是「即便出现无工作根的身份，判定也必须逐次交回人类」。
    """
    policy = AccessPolicy(mode=AccessMode.WORKSPACE, workspace=tmp_path, roots=())
    inside = tmp_path / "a.txt"
    assert decide_path_access(policy, inside, AccessOperation.READ) is AccessDecision.ASK
    assert decide_path_access(policy, inside, AccessOperation.WRITE) is AccessDecision.ASK


def test_decision_never_denies(tmp_path):
    """越界不是拒绝：判定只有放行与待决两种取值，越界以待决呈现。"""
    assert set(AccessDecision) == {AccessDecision.ALLOW, AccessDecision.ASK}
    policy = _workspace_policy(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    assert decide_path_access(policy, outside, AccessOperation.READ) is AccessDecision.ASK
    assert decide_path_access(policy, outside, AccessOperation.WRITE) is AccessDecision.ASK


def test_policy_from_context_defaults_to_workspace(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path)})
    assert policy.mode is AccessMode.WORKSPACE
    assert policy.workspace == tmp_path.resolve()
    assert policy.roots == (tmp_path.resolve(),)


def test_policy_from_context_reads_declared_mode(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path), "access_mode": "full"})
    assert policy.mode is AccessMode.FULL


def test_policy_from_context_treats_unknown_mode_as_workspace(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path), "access_mode": "bogus"})
    assert policy.mode is AccessMode.WORKSPACE


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
