r"""本文件验证 Agent Loop、Context Evolution、审计与 workspace API 的 OpenAPI 表面。

输入为 Gateway 组合根生成的 OpenAPI schema；输出为每个只读/控制/事件端点及严格 request schema 均可
发现的断言。具体工作流为不启动 lifespan，仅检查路由组合与文档生成。示例：
`pytest test_agent_loop_openapi.py`。
"""

from backend.app.gateway.app import app


def test_agent_loop_openapi_surface_is_complete() -> None:
    paths = app.openapi()["paths"]

    expected = {
        "/desktop/api/agent-loops": "post",
        "/desktop/api/agent-loops/{loop_id}": "get",
        "/desktop/api/agent-loops/{loop_id}/control": "post",
        "/desktop/api/agent-loops/{loop_id}/grant": "post",
        "/desktop/api/agent-loops/{loop_id}/override": "post",
        "/desktop/api/agent-loops/{loop_id}/decisions": "post",
        "/desktop/api/agent-loops/{loop_id}/events": "get",
        "/desktop/api/agent-loops/{loop_id}/events/stream": "get",
        "/desktop/api/agent-loops/{loop_id}/completion-evidence": "post",
        "/desktop/api/agent-loops/{loop_id}/audit": "get",
        "/desktop/api/workspaces/{workspace_id}/context-evolution": "get",
        "/desktop/api/workspaces/{workspace_id}/context-tree": "get",
        "/desktop/api/context-revisions/{revision_id}": "get",
        "/desktop/api/context-revisions/{revision_id}/message-provenance": "get",
        "/desktop/api/workspaces/{workspace_id}/slots": "get",
    }

    for path, method in expected.items():
        assert path in paths
        assert method in paths[path]


def test_loop_mutation_routes_require_documented_request_bodies() -> None:
    paths = app.openapi()["paths"]

    for path in (
        "/desktop/api/agent-loops",
        "/desktop/api/agent-loops/{loop_id}/control",
        "/desktop/api/agent-loops/{loop_id}/grant",
        "/desktop/api/agent-loops/{loop_id}/override",
        "/desktop/api/agent-loops/{loop_id}/decisions",
        "/desktop/api/agent-loops/{loop_id}/completion-evidence",
    ):
        assert "requestBody" in paths[path]["post"]
