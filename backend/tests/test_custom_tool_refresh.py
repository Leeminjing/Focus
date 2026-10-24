"""Verify capability refresh replaces custom tools only in newly assembled runs."""

import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace

from backend.app.desktop.composition.service import DesktopService
import focus.tools.registry as registry
from focus.tools.tools import get_available_tools


def _write_tool(root, name, version):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    entry = directory / "tool.py"
    entry.write_text(
        "from langchain_core.tools import tool\n"
        "@tool\n"
        f"def {name}(text: str) -> str:\n"
        "    '''Custom refresh probe.'''\n"
        f"    return '{version}:' + text\n"
        f"tool = {name}\n",
        encoding="utf-8",
    )
    return entry


def test_refresh_reloads_added_changed_removed_tools_and_keeps_existing_graph(monkeypatch, tmp_path):
    from langgraph.prebuilt import ToolNode
    import focus.tools.tools as discovery
    import focus.mcp.cache as mcp

    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "global"))
    monkeypatch.setattr(registry, "_registries", {})
    root = tmp_path / "global" / "tools"
    entry = _write_tool(root, "echo", "old")
    removed = _write_tool(root, "removed", "old")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    refresh_calls = []

    async def empty_tools(*args, **kwargs):
        return []

    async def refresh_mcp(path, *, force_refresh=False):
        refresh_calls.append((path, force_refresh))
        return []

    monkeypatch.setattr(discovery, "_builtin_tools", empty_tools)
    monkeypatch.setattr(discovery, "_plugin_tools", empty_tools)
    monkeypatch.setattr(discovery, "_mcp_tools", empty_tools)
    monkeypatch.setattr(mcp, "get_mcp_tools_cached", refresh_mcp)
    service = DesktopService.__new__(DesktopService)

    @asynccontextmanager
    async def session():
        yield object()

    async def entities(session, task_id):
        return None, SimpleNamespace(path=str(workspace))

    service.session_factory = session
    service.workspaces._get_task_entities = entities

    async def run():
        old_registry = registry.get_tool_registry()
        old_node = ToolNode([info.tool() for info in await get_available_tools(workspace=workspace)])
        # Equal-sized edits within the same timestamp must not reuse Python bytecode.
        original_stat = entry.stat()
        _write_tool(root, "echo", "new")
        os.utime(entry, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        removed.unlink()
        _write_tool(root, "added", "new")
        assert registry.get_tool_registry() is old_registry
        result = await service.refresh_task_capabilities("task")
        new_node = ToolNode([info.tool() for info in await get_available_tools(workspace=workspace)])
        assert set(new_node.tools_by_name) == {"echo", "added"}
        assert new_node.tools_by_name["echo"].invoke({"text": "x"}) == "new:x"
        assert new_node.tools_by_name["added"].invoke({"text": "x"}) == "new:x"
        assert set(old_node.tools_by_name) == {"echo", "removed"}
        assert old_node.tools_by_name["echo"].invoke({"text": "x"}) == "old:x"
        assert old_node.tools_by_name["removed"].invoke({"text": "x"}) == "old:x"
        assert registry.get_tool_registry() is not old_registry
        assert result["custom_tool_count"] == 2
        assert [skill["name"] for skill in result["skills"]] == ["focus-skill-to-strategy"]
        assert result["mcp_tool_count"] == 0
        assert result["failed_servers"] == [] and result["mcp_failures"] == []
        assert refresh_calls == [(str(workspace), True)]

    asyncio.run(run())
