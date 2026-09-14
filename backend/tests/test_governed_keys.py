"""受治理运行上下文键的声明并集、剥除与拒绝伪造用例。

输入为消费者声明与调用方提供的运行上下文；输出为受治理键集合与剥除结果。
工作流先锁定集合是跨模块声明的并集且不是手写名单，再锁定剥除只保留附带载荷，
最后在网关入口断言调用方伪造受治理字段的请求被拒绝、且不留下运行记录。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from focus.runtime.runs.manager import RunManager
from focus.security.governed import governed_keys, is_governed, strip_governed


def _import_consumers() -> None:
    """导入声明受治理键的消费者模块，使集合完整。"""
    import backend.app.desktop.collab  # noqa: F401
    import focus.agents.commitment.middleware  # noqa: F401
    import focus.agents.must_view  # noqa: F401
    import focus.security.context  # noqa: F401
    import focus.security.policy  # noqa: F401


def test_governed_keys_are_the_union_of_consumer_declarations():
    _import_consumers()
    keys = governed_keys()
    for expected in (
        "security_context",
        "workspace",
        "permissions",
        "access_mode",
        "agent_role",
        "thread_id",
        "workspace_id",
        "agent_id",
        "checkpoint_ns",
        "model_name",
        "allow_global_config",
        "must_view_materials",
        "model_supports_image_input",
        "task_id",
        "swarm_depth",
        "uploads",
    ):
        assert expected in keys, expected


def test_governed_set_is_not_a_hand_written_list():
    """集合来自声明并集：注册表模块内不存在任何键字面量（文档示例除外）。"""
    import ast

    path = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "security" / "governed.py"
    source = path.read_text(encoding="utf-8")
    assert "_GOVERNED: set[str] = set()" in source

    tree = ast.parse(source)
    docstrings = {
        doc
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for doc in [ast.get_docstring(node)]
        if doc
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }
    for key in ("must_view_materials", "checkpoint_ns", "swarm_depth", "access_mode"):
        assert key not in literals


def test_strip_governed_keeps_only_payload():
    _import_consumers()
    stripped = strip_governed(
        {
            "access_mode": "full",
            "workspace": "C:/",
            "permissions": ["read", "write", "host_command"],
            "checkpoint_ns": "swarm:victim",
            "uploads": "<current_uploads/>",
            "skills": ["a"],
        }
    )
    assert stripped == {"skills": ["a"]}


def test_strip_governed_tolerates_non_mappings():
    assert strip_governed(None) == {}
    assert strip_governed("not-a-mapping") == {}


def test_is_governed_answers_per_key():
    _import_consumers()
    assert is_governed("access_mode") is True
    assert is_governed("skills") is False


def test_governed_payload_cannot_overwrite_the_flat_projection():
    """附带载荷并入前先剥除受治理键，因此扁平键只可能来自安全上下文的投影。"""
    _import_consumers()
    from focus.security.context import (
        AuthorizationIdentity,
        ExecutionProfile,
        RoutingIdentity,
        derive_security_context,
        security_context_of,
    )
    from focus.security.policy import AccessMode, policy_from_context, workspace_roots

    workspace = Path.cwd()
    context = derive_security_context(
        ExecutionProfile(
            authorization=AuthorizationIdentity(
                workspace=workspace,
                roots=workspace_roots(workspace),
                permissions=("read",),
                access_mode=AccessMode.WORKSPACE,
                agent_role="main",
            ),
            routing=RoutingIdentity("thread-1", "ws-1", "main:task-1", ""),
        )
    ).to_runtime_context()
    forged = {
        **context,
        **strip_governed(
            {
                "access_mode": "full",
                "workspace": "C:/",
                "permissions": ["read", "write", "host_command"],
                "allow_global_config": True,
                "roots": ["C:/"],
                "skills": ["x"],
            }
        ),
    }
    assert forged["access_mode"] == "workspace"
    assert forged["workspace"] == str(workspace)
    assert forged["skills"] == ["x"]
    # 准入策略只认安全上下文：伪造的受治理字段与伪造的工作根都不构成第二个来源
    security = security_context_of(forged)
    assert security.authorization.access_mode is AccessMode.WORKSPACE
    assert security.authorization.workspace == workspace.resolve()
    assert security.authorization.roots == workspace_roots(workspace)
    policy = policy_from_context(forged)
    assert policy.mode is AccessMode.WORKSPACE
    assert policy.roots == workspace_roots(workspace)


def _gateway_request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                stream_bridge=object(),
                run_manager=RunManager(),
                checkpointer=None,
                store=None,
            )
        ),
        state=SimpleNamespace(current_user=None),
    )


def test_gateway_refuses_caller_supplied_governed_context():
    """调用方伪造受治理字段不生效：请求在登记运行之前被拒。"""
    from backend.app.gateway import services

    body = SimpleNamespace(
        context={
            "access_mode": "full",
            "permissions": ["read", "write", "host_command"],
            "workspace": "C:/",
            "checkpoint_ns": "swarm:victim",
        },
        input={"messages": [{"role": "human", "content": "x"}]},
        resume=None,
        stream_mode=["values"],
    )
    request = _gateway_request()
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(services.start_run(body, "thread-forged", request))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "execution_profile_required"
    assert request.app.state.run_manager.list_by_thread("thread-forged") == []
