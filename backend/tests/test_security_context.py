"""执行身份、安全上下文与内联单调派生的用例。

输入为执行身份档案、父级安全上下文与派生请求；输出为派生结果或拒绝。
工作流先锁定档案派生的归一与两类身份，再锁定扁平投影与策略来源，随后逐项锁定内联派生的
单调性（能力权限 / 工作根 / 访问模式 / 权柄面四项均只能收窄），最后断言派生工具不再从
运行上下文字典手工拼装字段。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from focus.security.context import (
    SECURITY_CONTEXT_KEY,
    AuthorizationIdentity,
    ChildRole,
    ExecutionProfile,
    RoutingIdentity,
    SecurityContext,
    derive_child_security_context,
    derive_security_context,
    has_security_context,
    security_context_of,
)
from focus.security.policy import AccessMode, policy_from_context

WORKSPACE = Path("C:/ws")
CHILD = Path("C:/ws/child")


def _profile(**overrides) -> ExecutionProfile:
    authorization = AuthorizationIdentity(
        workspace=WORKSPACE,
        roots=(WORKSPACE,),
        permissions=("read", "write"),
        access_mode=AccessMode.WORKSPACE,
        agent_role="main",
        authority=("tools",),
    )
    routing = RoutingIdentity(
        thread_id="thread-1", workspace_id="ws-1", agent_id="main:task-1", task_id="task-1", checkpoint_ns=""
    )
    values = {"authorization": authorization, "routing": routing, "owner": "user-1"}
    values.update(overrides)
    return ExecutionProfile(**values)


def _parent(mode: AccessMode = AccessMode.WORKSPACE, **overrides) -> SecurityContext:
    authorization = AuthorizationIdentity(
        workspace=WORKSPACE,
        roots=(WORKSPACE,),
        permissions=("read", "write"),
        access_mode=mode,
        agent_role="main",
        authority=("tools",),
    )
    authorization = authorization.__class__(**{**authorization.__dict__, **overrides})
    return derive_security_context(
        ExecutionProfile(
            authorization=authorization,
            routing=RoutingIdentity("thread-1", "ws-1", "main:task-1", "task-1", ""),
            owner="user-1",
        )
    )


def test_derivation_carries_both_identities_and_owner():
    context = derive_security_context(_profile())
    assert context.authorization.agent_role == "main"
    assert context.authorization.access_mode is AccessMode.WORKSPACE
    assert context.routing == RoutingIdentity("thread-1", "ws-1", "main:task-1", "task-1", "")
    assert context.owner == "user-1"


def test_derivation_normalises_roots_with_workspace_first():
    authorization = AuthorizationIdentity(
        workspace=WORKSPACE,
        roots=(WORKSPACE, WORKSPACE),
        permissions=("read",),
        access_mode=AccessMode.FULL,
        agent_role="main",
    )
    context = derive_security_context(
        ExecutionProfile(authorization=authorization, routing=RoutingIdentity("t", "w", "a", "task", ""))
    )
    assert context.authorization.roots == (WORKSPACE,)
    assert context.authorization.permissions == ("read",)


def test_derivation_rejects_relative_workspace():
    authorization = AuthorizationIdentity(
        workspace=Path("relative/ws"),
        roots=(Path("relative/ws"),),
        permissions=("read",),
        access_mode=AccessMode.WORKSPACE,
        agent_role="main",
    )
    with pytest.raises(ValueError, match="绝对路径"):
        derive_security_context(
            ExecutionProfile(authorization=authorization, routing=RoutingIdentity("t", "w", "a", "task", ""))
        )


def test_runtime_context_projection_carries_reserved_key_and_flat_keys():
    context = derive_security_context(_profile())
    runtime = context.to_runtime_context()
    assert runtime[SECURITY_CONTEXT_KEY] is context
    assert runtime["workspace"] == str(WORKSPACE)
    assert runtime["permissions"] == ["read", "write"]
    assert runtime["access_mode"] == "workspace"
    assert runtime["agent_role"] == "main"
    assert runtime["checkpoint_ns"] == ""
    assert runtime["user_id"] == "user-1"


def test_runtime_context_projection_omits_absent_owner():
    context = derive_security_context(_profile(owner=None))
    assert "user_id" not in context.to_runtime_context()


def test_security_context_of_requires_the_reserved_key():
    assert has_security_context({}) is False
    with pytest.raises(RuntimeError, match="受治理安全上下文"):
        security_context_of({"workspace": "C:/ws"})
    with pytest.raises(RuntimeError, match="受治理安全上下文"):
        security_context_of(None)


def test_policy_comes_from_the_security_context_not_from_flat_keys():
    """扁平键与安全上下文冲突时以安全上下文为准，扁平键不构成第二个来源。"""
    context = derive_security_context(_profile())
    runtime = context.to_runtime_context()
    runtime["access_mode"] = "full"
    runtime["workspace"] = "C:/somewhere-else"
    policy = policy_from_context(runtime)
    assert policy.mode is AccessMode.WORKSPACE
    assert policy.workspace == WORKSPACE


def test_child_derivation_inherits_by_default():
    parent = _parent()
    child = derive_child_security_context(parent, ChildRole.SPAWN_AGENT)
    assert child.authorization.permissions == parent.authorization.permissions
    assert child.authorization.roots == parent.authorization.roots
    assert child.authorization.access_mode is parent.authorization.access_mode
    assert child.authorization.authority == parent.authorization.authority
    assert child.authorization.agent_role == "spawn_agent"
    assert child.routing == parent.routing
    assert child.owner == parent.owner


def test_child_derivation_allows_narrowing():
    parent = _parent(mode=AccessMode.FULL)
    child = derive_child_security_context(
        parent,
        ChildRole.SPAWN_AGENT,
        permissions=("read",),
        roots=(CHILD,),
        access_mode=AccessMode.WORKSPACE,
        authority=(),
    )
    assert child.authorization.permissions == ("read",)
    assert child.authorization.roots == (CHILD,)
    assert child.authorization.access_mode is AccessMode.WORKSPACE
    assert child.authorization.authority == ()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"permissions": ("read", "write", "host_command")}, "能力权限"),
        ({"roots": (Path("C:/other"),)}, "工作根"),
        ({"roots": (WORKSPACE, Path("C:/other"))}, "工作根"),
        ({"authority": ("tools", "plugins")}, "权柄面"),
    ],
)
def test_child_derivation_rejects_widening(overrides, message):
    parent = _parent()
    with pytest.raises(ValueError, match=message):
        derive_child_security_context(parent, ChildRole.SPAWN_AGENT, **overrides)


def test_child_derivation_rejects_wider_access_mode():
    parent = _parent(mode=AccessMode.WORKSPACE)
    with pytest.raises(ValueError, match="访问模式"):
        derive_child_security_context(parent, ChildRole.SPAWN_AGENT, access_mode=AccessMode.FULL)


def test_spawn_agent_derives_instead_of_assembling_from_the_context_dict():
    """派生工具不再从运行上下文手工拼装字段；缺失安全上下文即失败。"""
    from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool

    tool = build_spawn_agent_tool()
    runtime = ToolRuntime(
        state={},
        context={"workspace": "C:/ws", "permissions": ["read", "write"]},
        config={},
        stream_writer=None,
        tool_call_id=None,
        store=None,
        tools=[],
    )
    import asyncio

    with pytest.raises(RuntimeError, match="受治理安全上下文"):
        asyncio.run(tool.ainvoke({"type": "tool_call", "id": "c1", "name": "spawn_agent",
                                  "args": {"task": "x", "runtime": runtime}}))


def test_spawn_agent_source_holds_no_manual_context_assembly():
    path = (
        Path(__file__).parents[1]
        / "packages" / "harness" / "focus" / "tools" / "builtins" / "spawn_agent_tool.py"
    )
    source = path.read_text(encoding="utf-8")
    for forbidden in ('context.get("permissions")', 'context.get("model_name")', 'context.get("workspace")'):
        assert forbidden not in source
    assert "derive_child_security_context" in source


def test_launch_points_derive_the_governed_context():
    """两个启动面（桌面服务与空间插件）都改走统一组装入口：不再手工拼装受治理字段或执行命名空间。"""
    root = Path(__file__).parents[2]
    desktop = (root / "backend/app/desktop/service.py").read_text(encoding="utf-8")
    spatial = (root / "plugins/spatial-patrol/routes.py").read_text(encoding="utf-8")

    assert "langgraph_context = {" not in desktop
    assert desktop.count("self._governed_context(") >= 3
    # 插件启动点同样经统一组装入口 + 自己的服务端生产者写回，不再铺开身份投影
    assert "assemble_run_context(" in spatial
    assert "project_spatial_context(" in spatial
    assert "**derive_security_context(" not in spatial
    assert '"permissions": permissions,' not in spatial


def test_execution_namespace_comes_from_the_durable_record():
    """执行命名空间以持久化身份为准：启动点不再临时拼接。"""
    root = Path(__file__).parents[2]
    desktop = (root / "backend/app/desktop/service.py").read_text(encoding="utf-8")
    spatial = (root / "plugins/spatial-patrol/routes.py").read_text(encoding="utf-8")

    assert 'f"swarm:{agent_id}"' not in desktop
    assert "agent_row.checkpoint_ns" in desktop
    assert 'f"patrol:{anchor.spatial_id}"' not in spatial
    assert "anchor.checkpoint_ns" in spatial


def test_swarm_agent_namespace_is_persisted_and_unique():
    """swarm 的执行命名空间自建表起就是持久化且唯一的，因此（会话标识 + 命名空间）唯一。"""
    from backend.app.desktop.models import SwarmAgent

    assert SwarmAgent.__table__.c.checkpoint_ns.unique is True

    root = Path(__file__).parents[1]
    migration = (
        root / "packages" / "harness" / "focus" / "persistence" / "migrations"
        / "versions" / "f6a7b8c9d0e1_create_swarm_agents_table.py"
    ).read_text(encoding="utf-8")
    assert '"checkpoint_ns"' in migration
    assert "unique=True" in migration
    assert 'op.create_index("ix_swarm_agents_task_id"' in migration


def test_spatial_anchor_namespace_is_derived_from_persisted_identity():
    """空间载体的命名空间由持久化主键派生，属于锚点身份而非运行期临时拼接。"""
    from plugins.spatial_patrol.models import SpatialAnchor

    anchor = SpatialAnchor(
        spatial_id="abc123", task_id="task-1", content_ref="page.pdf", x=0.1, y=0.2
    )
    assert anchor.checkpoint_ns == "patrol:abc123"


def test_owner_comes_from_the_execution_profile_only():
    """归属只来自执行身份档案；源码侧不再读取一个从不被赋值的请求态身份。"""
    root = Path(__file__).parents[2]
    gateway = (root / "backend/app/gateway/services.py").read_text(encoding="utf-8")
    worker = (
        root / "backend/packages/harness/focus/runtime/runs/worker.py"
    ).read_text(encoding="utf-8")

    assert "current_user" not in gateway
    assert "user_id = security.owner" in worker
    assert 'langgraph_context.get("user_id")' not in worker


def test_capabilities_requiring_owner_are_not_assumed_available():
    """无归属的执行身份：上下文不携带 user_id，归属显式为空而不是被假定可用。"""
    context = derive_security_context(_profile(owner=None))
    assert context.owner is None
    assert "user_id" not in context.to_runtime_context()
    assert security_context_of(context.to_runtime_context()).owner is None
