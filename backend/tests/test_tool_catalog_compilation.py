"""本文件对外提供目录编译的来源、唯一性与绑定保真验证。

输入为普通／中间件工具与带来源发现记录；输出为冲突位置、有效顺序和 metadata 身份断言。
工作流为编译声明前验证重复，检查所有输入位置，再验证发现与执行选择没有混淆。
示例：pytest backend/tests/test_tool_catalog_compilation.py；不调用模型或工具副作用。
"""

from dataclasses import FrozenInstanceError

import pytest
from langchain.agents.middleware import AgentMiddleware

from backend.tests.tool_catalog_support import test_tool
from focus.security.effects import NO_LOCAL_EFFECT, OPAQUE_LOCAL_EFFECT, effect_of
from focus.tools.catalog import ToolNameConflict, compile_agent_tool_catalog, execution_pool_tools
from focus.tools.interfaces import ToolInfo


def _info(tool, source):
    return ToolInfo(tool.name, tool.name, tool.description, source, lambda: tool)


def test_source_selection_and_framework_order_preserve_original_bindings():
    calls = []
    builtin, custom, mcp, plugin = [test_tool(name, calls) for name in ("builtin", "custom", "mcp", "plugin")]
    discovered = [_info(item, item.name) for item in (builtin, custom, mcp, plugin)]
    middleware = AgentMiddleware()
    middleware.tools = [plugin]
    for include_builtin, ordinary in [(True, [builtin, custom, mcp]), (False, [custom, mcp])]:
        selected = execution_pool_tools(discovered, include_builtin=include_builtin)
        catalog = compile_agent_tool_catalog(selected, [middleware])
        assert catalog.tools == (plugin, *ordinary)
        assert all(a is b for a, b in zip(catalog.regular_tools, ordinary))
        assert [binding.source for binding in catalog.regular_bindings] == [tool.name for tool in ordinary]
        assert all(effect_of(tool) is NO_LOCAL_EFFECT for tool in catalog.tools)
        changed = catalog.regular_tools
        changed.clear()
        assert catalog.regular_tools == ordinary
        with pytest.raises(FrozenInstanceError):
            catalog.regular_bindings = ()
    assert [entry.name for entry in discovered] == ["builtin", "custom", "mcp", "plugin"]


@pytest.mark.parametrize("kind", ["ordinary", "middleware", "cross"])
@pytest.mark.parametrize("same_object", [True, False])
def test_duplicate_declarations_report_both_owners_before_name_mapping(kind, same_object):
    calls = []
    first = test_tool("collision", calls)
    second = first if same_object else test_tool("collision", calls, effect=OPAQUE_LOCAL_EFFECT)
    middleware = AgentMiddleware()
    ordinary = [first, second] if kind == "ordinary" else [first] if kind == "cross" else []
    middleware.tools = [first, second] if kind == "middleware" else [second] if kind == "cross" else []
    with pytest.raises(ToolNameConflict) as caught:
        compile_agent_tool_catalog(ordinary, [middleware])
    assert caught.value.tool_name == "collision"
    first_owner, second_owner = caught.value.owners
    assert first_owner != second_owner
    assert all(owner in str(caught.value) for owner in caught.value.owners)
    assert "collision" in str(caught.value) and not calls


def test_legacy_raw_discovery_is_not_classified_by_its_name():
    item = test_tool("plugin", [])
    catalog = compile_agent_tool_catalog(execution_pool_tools([item], include_builtin=False), [])
    assert catalog.regular_tools == [item] and catalog.regular_bindings[0].source is None
