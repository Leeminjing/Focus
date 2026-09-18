"""本文件对外提供测试用的运行上下文构造入口 runtime_context 与 tool_runtime，使消费者用例与
生产走同一条组装路径。

输入为执行主体身份、任务身份、工作区与可选的权限 / 访问模式 / 执行提示 / 本轮材料；输出为完整的
运行上下文扁平字典（受治理键只来自身份投影与已声明的服务端生产者），或携带该上下文的 ToolRuntime。
具体工作流为 由测试参数构造执行身份档案 → 调用生产同源的 assemble_run_context（身份投影 + 剥除后的
载荷 + 执行提示写回）→ 需要材料时再按生产者写回材料投影。

示例：
    runtime = tool_runtime(agent_id="main:t-1", task_id="t-1")
    context = runtime_context(agent_id="swarm:abc", task_id="t-1", swarm_depth=2)
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime

from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context
from focus.security.context import AuthorizationIdentity, ExecutionProfile, RoutingIdentity
from focus.security.launch import assemble_run_context
from focus.security.policy import AccessMode, workspace_roots

DEFAULT_WORKSPACE = "C:/ws"

__all__ = ["DEFAULT_WORKSPACE", "runtime_context", "tool_runtime"]


def runtime_context(
    *,
    agent_id: str,
    task_id: str,
    workspace: str = DEFAULT_WORKSPACE,
    workspace_id: str = "ws-1",
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    permissions: tuple[str, ...] = ("read",),
    access_mode: AccessMode = AccessMode.WORKSPACE,
    agent_role: str = "main",
    swarm_depth: int = 0,
    materials: RunMaterialInputs | None = None,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一份完整的运行上下文；受治理键与生产同源，不使用手写字典。"""
    workspace_path = Path(workspace).resolve()
    profile = ExecutionProfile(
        authorization=AuthorizationIdentity(
            workspace=workspace_path,
            roots=workspace_roots(workspace_path),
            permissions=tuple(permissions),
            access_mode=access_mode,
            agent_role=agent_role,
        ),
        routing=RoutingIdentity(
            thread_id=thread_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            task_id=task_id,
            checkpoint_ns="",
            run_id=run_id,
        ),
    )
    context = assemble_run_context(profile, dict(extras or {}), dispatch_hints={"swarm_depth": swarm_depth})
    if materials is not None:
        project_run_material_context(context, materials, False)
    return context


def tool_runtime(**kwargs: Any) -> ToolRuntime:
    """构造携带运行上下文的 ToolRuntime（用于直接 ainvoke 工具的场景）。"""
    return ToolRuntime(
        state={},
        context=runtime_context(**kwargs),
        config={},
        stream_writer=None,
        tool_call_id=None,
        store=None,
        tools=[],
    )
