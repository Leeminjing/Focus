"""外部执行工具信任分层的用例。

输入为外来自声明、随版本契约、用户显式覆盖与受治理上下文；输出为签发后的效果契约与准入结论。
工作流先锁定外来自声明不参与判定、随版本契约优先于用户覆盖、用户覆盖的取值域被收窄，
再锁定信任覆盖所在的配置属于权柄面，最后锁定本系统自行接线的 Context7 不产生待决。
"""

import asyncio
import logging
from pathlib import Path

import pytest
from langchain_core.tools import tool

from focus.security.authority import AuthorityKind, authority_surfaces
from focus.security.effects import (
    NO_LOCAL_EFFECT,
    ToolEffectKind,
    declare_effect,
    effect_of,
    has_declared_effect,
    structured_fs,
)
from focus.security.policy import (
    AccessDecision,
    AccessOperation,
    AccessPolicy,
    AccessMode,
    decide_path_access,
)
from backend.tests.runtime_context_support import tool_runtime
from focus.security.trust import (
    CONTEXT7_SERVER,
    TRUST_VALUES,
    parse_trust_override,
    shipped_contract,
    trust_tools,
)


@tool
def external_probe(value: str) -> str:
    """A tool pretending to be an external executor."""
    return value


def _probe():
    return external_probe.model_copy(deep=True)


def _named(name: str):
    tool_ = _probe()
    tool_.name = name
    return tool_


def _capture(logger_name: str) -> tuple[list[logging.LogRecord], logging.Handler]:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logging.getLogger(logger_name).addHandler(handler)
    return records, handler


def test_external_self_declaration_does_not_change_the_verdict():
    """外部自声明只作提示：摄取后既不留存其放宽声明，也不会据此放宽。"""
    hostile = _probe()
    declare_effect(hostile, NO_LOCAL_EFFECT)

    [trusted] = trust_tools("unknown-server", [hostile])

    assert effect_of(trusted).kind is ToolEffectKind.OPAQUE_LOCAL
    assert has_declared_effect(trusted) is False


def test_self_declared_structured_effect_is_discarded_too():
    """自称为可结构化枚举同样不成立：没有 Focus 侧的解析器就不是结构化。"""
    hostile = _probe()
    declare_effect(hostile, structured_fs(lambda args, context: None))

    [trusted] = trust_tools("unknown-server", [hostile])

    assert effect_of(trusted).kind is ToolEffectKind.OPAQUE_LOCAL


def test_shipped_contract_outranks_user_override():
    """随版本契约优先于用户覆盖：自接 server 的信任不由用户配置决定。"""
    assert shipped_contract(CONTEXT7_SERVER, "query-docs") is NO_LOCAL_EFFECT

    [trusted] = trust_tools(
        CONTEXT7_SERVER,
        [_named("query-docs")],
        parse_trust_override(CONTEXT7_SERVER, {"*": "opaque_local"}),
    )

    assert effect_of(trusted).kind is ToolEffectKind.NO_LOCAL_EFFECT


def test_user_override_relaxes_only_within_the_allowed_domain():
    """用户覆盖只能在不透明与无本地效果之间选择；越界取值被拒并保持最严。"""
    assert set(TRUST_VALUES) == {"no_local_effect", "opaque_local"}

    allowed = parse_trust_override("user-server", {"external_probe": "no_local_effect"})
    [relaxed] = trust_tools("user-server", [_probe()], allowed)
    assert effect_of(relaxed).kind is ToolEffectKind.NO_LOCAL_EFFECT

    records, handler = _capture("focus.security.trust")
    try:
        rejected = parse_trust_override(
            "user-server", {"probe": "structured_fs", "other": "delegated_execution"}
        )
    finally:
        logging.getLogger("focus.security.trust").removeHandler(handler)

    assert rejected == {}
    assert len(records) == 2
    [trusted] = trust_tools("user-server", [_probe()], rejected)
    assert effect_of(trusted).kind is ToolEffectKind.OPAQUE_LOCAL


def test_wildcard_override_covers_every_tool_of_its_server():
    """`*` 覆盖该 server 的全部工具。"""
    override = parse_trust_override("user-server", {"*": "no_local_effect"})

    trusted = trust_tools("user-server", [_named("alpha"), _named("beta")], override)

    assert [effect_of(tool_).kind for tool_ in trusted] == [
        ToolEffectKind.NO_LOCAL_EFFECT,
        ToolEffectKind.NO_LOCAL_EFFECT,
    ]


def test_overrides_are_scoped_to_the_server_that_declared_them(monkeypatch):
    """覆盖按 server 归档：只作用于声明它的那个 server，未声明的仍保持最严。"""
    pytest.importorskip("langchain_mcp_adapters.client")
    import focus.mcp.tools as mcp_tools
    from focus.config.extensions_config import ExtensionsConfig

    class _FakeSession:
        def __init__(self, client, server_name, load_tools):
            self._tools = [_probe()]

        async def start(self):
            return self._tools

        async def close(self):
            return None

    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "declared": {
                    "enabled": True, "type": "http", "description": "有覆盖",
                    "url": "https://declared.example/mcp", "trust": {"*": "no_local_effect"},
                },
                "silent": {
                    "enabled": True, "type": "http", "description": "无覆盖",
                    "url": "https://silent.example/mcp",
                },
            }
        }
    )
    servers = {
        "declared": {"transport": "http", "url": "https://declared.example/mcp"},
        "silent": {"transport": "http", "url": "https://silent.example/mcp"},
    }
    monkeypatch.setattr(mcp_tools, "_PersistentMcpSession", _FakeSession)
    monkeypatch.setattr(mcp_tools, "get_extensions_config", lambda _path: config)
    monkeypatch.setattr(mcp_tools, "build_servers_config", lambda _config: servers)

    loaded = asyncio.run(mcp_tools.get_mcp_tools())

    assert [effect_of(tool_).kind for tool_ in loaded] == [
        ToolEffectKind.NO_LOCAL_EFFECT,
        ToolEffectKind.OPAQUE_LOCAL,
    ]


def test_trust_override_config_is_an_authority_surface(tmp_path):
    """能放宽信任的持久化配置本身属于权柄面：写入它请求人工决定。"""
    surfaces = authority_surfaces(tmp_path)
    trust_surfaces = [
        surface for surface in surfaces if surface.path.name == "extensions_config.json"
    ]
    assert len(trust_surfaces) == 2
    assert all(surface.kind is AuthorityKind.TRUST for surface in trust_surfaces)

    policy = AccessPolicy(
        mode=AccessMode.WORKSPACE,
        workspace=tmp_path.resolve(),
        roots=(tmp_path.resolve(),),
        authority=surfaces,
    )
    for surface in trust_surfaces:
        assert (
            decide_path_access(policy, surface.path, AccessOperation.WRITE) is AccessDecision.ASK
        )


def test_promise_stage_external_queries_do_not_ask(monkeypatch):
    """承诺层自接的 Context7 属于随版本契约，其查询在工作区保护下也不产生待决。"""
    pytest.importorskip("langchain_mcp_adapters.client")
    import focus.mcp.tools as mcp_tools

    resolver, query = _context7_tools()

    class _FakeSession:
        def __init__(self, client, server_name, load_tools):
            self._tools = [resolver, query]

        async def start(self):
            return self._tools

        async def close(self):
            return None

    monkeypatch.setattr(mcp_tools, "_PersistentMcpSession", _FakeSession)

    from focus.mcp.context7 import get_context7_tools

    loaded = asyncio.run(get_context7_tools("https://context7.example/mcp"))

    assert [tool_.name for tool_ in loaded] == ["resolve-library-id", "query-docs"]
    for tool_ in loaded:
        assert effect_of(tool_).kind is ToolEffectKind.NO_LOCAL_EFFECT

    from focus.security.middleware import _admit
    from langchain.tools import ToolRuntime
    from langchain.tools.tool_node import ToolCallRequest

    runtime = tool_runtime(
        agent_id="main:t-mcp", task_id="t-mcp", workspace=str(Path.cwd()),
    )
    for tool_ in loaded:
        request = ToolCallRequest(
            tool_call={"name": tool_.name, "args": {}, "id": "c1"},
            tool=tool_, state={}, runtime=runtime,
        )
        assert _admit(request).asked is False


def _context7_tools():
    from langchain_core.tools import tool as make_tool

    @make_tool
    def resolve_library_id(libraryName: str) -> str:
        """Resolve a library id."""
        return libraryName

    @make_tool
    def query_docs(libraryId: str) -> str:
        """Query official docs."""
        return libraryId

    resolve_library_id.name = "resolve-library-id"
    query_docs.name = "query-docs"
    return resolve_library_id, query_docs
