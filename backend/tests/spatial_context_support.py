"""本文件对外提供空间插件用例的运行上下文构造入口 spatial_tool_runtime。

输入为空间锚点字段（spatial_id / content_ref / page / x / y）、工作区、权限、变更证据、DOCX 候选与会话
身份；输出为携带该上下文的 ToolRuntime。具体工作流为经与生产同源的组装入口产出基础上下文（身份投影
+ 剥除后的载荷），再由插件自己的服务端生产者写回空间与 DOCX 的受治理键——与 routes.py 的启动路径逐字
一致，因此用例测的是生产形状。

示例：
    runtime = spatial_tool_runtime(workspace=tmp_path, y=3.0, change_evidence=evidence)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain.tools import ToolRuntime

from backend.tests.runtime_context_support import runtime_context
from plugins.spatial_patrol.spatial_context import project_spatial_context

__all__ = ["spatial_tool_runtime"]


def spatial_tool_runtime(
    *,
    workspace: str | Path,
    y: float,
    content_ref: str = "sample.docx",
    page: int = 1,
    x: float = 0.5,
    spatial_id: str = "spatial-1",
    task_id: str = "t-spatial",
    workspace_id: str = "ws-spatial",
    thread_id: str = "thread-spatial",
    run_id: str = "run-spatial",
    permissions: tuple[str, ...] = ("read", "write"),
    change_evidence: dict[str, Any] | None = None,
    docx_candidates: dict[str, Any] | None = None,
    docx_session: Any | None = None,
    extras: dict[str, Any] | None = None,
) -> ToolRuntime:
    """构造携带生产同源空间上下文的 ToolRuntime。"""
    context = runtime_context(
        agent_id=spatial_id,
        task_id=task_id,
        workspace=str(workspace),
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        permissions=tuple(permissions),
        agent_role="patrol",
        extras=extras,
    )
    anchor = SimpleNamespace(
        spatial_id=spatial_id, task_id=task_id, content_ref=content_ref, page=page, x=x, y=y,
    )
    project_spatial_context(
        context,
        anchor,
        change_evidence=change_evidence,
        docx_candidates=docx_candidates,
        docx_session=docx_session,
    )
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=None,
        tool_call_id=None, store=None, tools=[],
    )
