"""工具效果契约的类型、签发、默认值与结构化目标解析用例。

输入为工具对象、契约与受治理上下文；输出为契约解析结果与本地目标集合。
工作流先锁定「未签发即不透明」的默认语义与契约自校验，再锁定内置工具的逐项分类覆盖，
最后覆盖进程外第三方工具自带声明的处理与结构化解析的确定性要求。
"""

import asyncio

import pytest
from langchain_core.tools import tool

from focus.security.effects import (
    DELEGATED_EXECUTION_EFFECT,
    EFFECT_METADATA_KEY,
    NO_LOCAL_EFFECT,
    OPAQUE_LOCAL_EFFECT,
    EffectContract,
    ResolvedFsEffect,
    ToolEffectKind,
    declare_all_effects,
    declare_effect,
    discard_declared_effect,
    effect_of,
    has_declared_effect,
    resolve_fs_effect,
    structured_fs,
)


@tool
def plain(value: str) -> str:
    """A tool that declares nothing."""
    return value


def _two_targets(args, context):
    return ResolvedFsEffect(reads=(context["root"],), writes=(context["root"], args["dst"]))


def test_undeclared_tool_is_opaque():
    assert effect_of(plain).kind is ToolEffectKind.OPAQUE_LOCAL
    assert has_declared_effect(plain) is False


def test_effect_of_none_is_opaque():
    assert effect_of(None).kind is ToolEffectKind.OPAQUE_LOCAL


def test_declared_effect_round_trips():
    declare_effect(plain, NO_LOCAL_EFFECT)
    assert effect_of(plain).kind is ToolEffectKind.NO_LOCAL_EFFECT
    assert has_declared_effect(plain) is True


def test_declared_opaque_is_distinguishable_from_undeclared():
    other = plain.model_copy(deep=True)
    declare_effect(other, OPAQUE_LOCAL_EFFECT)
    assert effect_of(other).kind is ToolEffectKind.OPAQUE_LOCAL
    assert has_declared_effect(other) is True


def test_declare_all_effects_preserves_order():
    first = plain.model_copy(deep=True)
    second = plain.model_copy(deep=True)
    declared = declare_all_effects([first, second], DELEGATED_EXECUTION_EFFECT)
    assert declared == [first, second]
    assert all(effect_of(item).kind is ToolEffectKind.DELEGATED_EXECUTION for item in declared)


def test_structured_contract_requires_resolver():
    with pytest.raises(ValueError, match="解析器"):
        EffectContract(ToolEffectKind.STRUCTURED_FS)


def test_only_structured_contract_may_carry_resolver():
    with pytest.raises(ValueError, match="解析器"):
        EffectContract(ToolEffectKind.NO_LOCAL_EFFECT, _two_targets)


def test_resolve_returns_empty_for_non_structured_contract():
    assert resolve_fs_effect(NO_LOCAL_EFFECT, {}, {}) == ResolvedFsEffect()
    assert resolve_fs_effect(OPAQUE_LOCAL_EFFECT, {"path": "a"}, {"root": "r"}) == ResolvedFsEffect()


def test_resolve_returns_multiple_targets():
    contract = structured_fs(_two_targets)
    resolved = resolve_fs_effect(contract, {"dst": "b"}, {"root": "a"})
    assert resolved.reads == ("a",)
    assert resolved.writes == ("a", "b")


def test_resolve_is_deterministic_and_idempotent():
    contract = structured_fs(_two_targets)
    args = {"dst": "b"}
    context = {"root": "a"}
    assert resolve_fs_effect(contract, args, context) == resolve_fs_effect(contract, args, context)


def test_resolver_must_return_resolved_effect():
    contract = structured_fs(lambda args, context: ("wrong",))
    with pytest.raises(TypeError, match="ResolvedFsEffect"):
        resolve_fs_effect(contract, {}, {})


def test_third_party_self_declaration_does_not_survive_ingestion():
    external = plain.model_copy(deep=True)
    external.metadata = {EFFECT_METADATA_KEY: NO_LOCAL_EFFECT}
    assert effect_of(external).kind is ToolEffectKind.NO_LOCAL_EFFECT
    discard_declared_effect(external)
    assert effect_of(external).kind is ToolEffectKind.OPAQUE_LOCAL
    assert has_declared_effect(external) is False


def test_mcp_aggregation_preserves_the_contract_signed_at_ingestion(monkeypatch):
    """聚合层不重新解释 MCP 效果契约：摄取边界已签发，再清除会抹掉随版本契约。"""
    import focus.tools.tools as tools_module

    signed = plain.model_copy(deep=True)
    signed.name = "external_tool"
    declare_effect(signed, NO_LOCAL_EFFECT)

    async def _fake_cached():
        return [signed]

    monkeypatch.setattr("focus.mcp.cache.get_mcp_tools_cached", _fake_cached)
    infos = asyncio.run(tools_module._mcp_tools())

    assert [info.name for info in infos] == ["external_tool"]
    assert effect_of(infos[0].tool()).kind is ToolEffectKind.NO_LOCAL_EFFECT


def _builtin_contracts():
    from focus.skills.catalog import SkillCatalog
    from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool
    from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool
    from focus.tools.builtins.web_tools import WEB_TOOLS
    from focus.tools.builtins.workspace_tools import WORKSPACE_TOOLS

    tools = [
        *WORKSPACE_TOOLS,
        *WEB_TOOLS,
        build_describe_skill_tool(SkillCatalog()),
        build_spawn_agent_tool(),
    ]
    return {item.name: item for item in tools}


def test_every_builtin_tool_has_a_declared_effect():
    undeclared = [name for name, item in _builtin_contracts().items() if not has_declared_effect(item)]
    assert undeclared == []


def test_file_tools_are_structured_with_path_targets(tmp_path):
    from focus.tools.builtins.workspace_tools import WORKSPACE_TOOLS

    by_name = {item.name: item for item in WORKSPACE_TOOLS}
    context = {"workspace": str(tmp_path)}

    read_effect = resolve_fs_effect(effect_of(by_name["read_file"]), {"path": "a.txt"}, context)
    assert read_effect.reads == ((tmp_path / "a.txt").resolve(),)
    assert read_effect.writes == ()

    list_effect = resolve_fs_effect(effect_of(by_name["list_files"]), {"path": "sub"}, context)
    assert list_effect.reads == ((tmp_path / "sub").resolve(),)

    write_effect = resolve_fs_effect(effect_of(by_name["write_file"]), {"path": "b.txt"}, context)
    assert write_effect.writes == ((tmp_path / "b.txt").resolve(),)
    assert write_effect.reads == ()


def test_shell_tools_are_explicitly_opaque():
    from focus.tools.builtins.workspace_tools import WORKSPACE_TOOLS

    shells = [item for item in WORKSPACE_TOOLS if item.name in {"bash", "powershell", "cmd", "sh"}]
    assert len(shells) == 4
    for item in shells:
        assert has_declared_effect(item) is True
        assert effect_of(item).kind is ToolEffectKind.OPAQUE_LOCAL


def test_network_and_skill_tools_have_no_local_effect():
    contracts = _builtin_contracts()
    for name in ("web_search", "web_fetch", "describe_skill"):
        assert effect_of(contracts[name]).kind is ToolEffectKind.NO_LOCAL_EFFECT


def test_spawn_agent_creates_a_delegated_execution():
    assert effect_of(_builtin_contracts()["spawn_agent"]).kind is ToolEffectKind.DELEGATED_EXECUTION


def _assert_all_declared(tools, expected: ToolEffectKind):
    assert tools
    for item in tools:
        assert has_declared_effect(item), item.name
        assert effect_of(item).kind is expected, item.name


def test_collab_tools_have_no_local_effect():
    from backend.app.desktop.collab import AgentCollab

    collab = AgentCollab(session_factory=None)
    tools = [
        *collab.build_collab_tools("main"),
        *collab.build_collab_tools("teammate"),
        *collab.build_collab_tools("worker"),
    ]
    _assert_all_declared(tools, ToolEffectKind.NO_LOCAL_EFFECT)
    assert collab.build_collab_tools("patrol") == []


def test_desktop_spawn_tools_are_delegated_and_wait_is_effect_free():
    from backend.app.desktop.service import DesktopService

    service = DesktopService.__new__(DesktopService)
    kinds = {item.name: effect_of(item).kind for item in service._build_swarm_tools()}
    assert kinds["spawn_teammate"] is ToolEffectKind.DELEGATED_EXECUTION
    assert kinds["spawn_worker"] is ToolEffectKind.DELEGATED_EXECUTION
    assert kinds["wake_agent"] is ToolEffectKind.DELEGATED_EXECUTION
    assert kinds["wait_for_swarm"] is ToolEffectKind.NO_LOCAL_EFFECT


def test_desktop_reader_tools_have_no_local_effect():
    from backend.app.desktop.service import DesktopService

    service = DesktopService.__new__(DesktopService)
    tools = [*service._build_patrol_reader_tools("task-1"), *service._build_swarm_reader_tools("task-1")]
    _assert_all_declared(tools, ToolEffectKind.NO_LOCAL_EFFECT)


def test_spatial_observation_tools_are_structured_on_the_carrier(tmp_path):
    from plugins.spatial_patrol.spatial import ObservationService, build_observation_tools

    carrier = tmp_path / "page.pdf"
    carrier.write_bytes(b"%PDF-1.4\n")
    tools = build_observation_tools(ObservationService({}, None))
    _assert_all_declared(tools, ToolEffectKind.STRUCTURED_FS)

    context = {"workspace": str(tmp_path), "content_ref": "page.pdf"}
    for item in tools:
        resolved = resolve_fs_effect(effect_of(item), {"radius": 0.1}, context)
        assert resolved.reads == (carrier.resolve(),)


def test_docx_tools_are_structured_on_the_carrier(tmp_path):
    from plugins.spatial_patrol.docx_edit import (
        delete_docx_paragraph,
        observe_docx_delete_candidate,
    )
    from plugins.spatial_patrol.docx_tools import apply_docx_edit, observe_docx_target

    carrier = tmp_path / "doc.docx"
    carrier.write_bytes(b"placeholder")
    context = {"workspace": str(tmp_path), "content_ref": "doc.docx"}

    readers = [observe_docx_target, observe_docx_delete_candidate]
    writers = [apply_docx_edit, delete_docx_paragraph]
    _assert_all_declared(readers, ToolEffectKind.STRUCTURED_FS)
    _assert_all_declared(writers, ToolEffectKind.STRUCTURED_FS)

    for item in readers:
        resolved = resolve_fs_effect(effect_of(item), {}, context)
        assert resolved.reads == (carrier.resolve(),)
        assert resolved.writes == ()
    for item in writers:
        resolved = resolve_fs_effect(effect_of(item), {}, context)
        assert resolved.writes == (carrier.resolve(),)


def test_structured_resolver_fails_loudly_without_governed_context(tmp_path):
    from plugins.spatial_patrol.docx_tools import observe_docx_target

    with pytest.raises(RuntimeError, match="载体上下文"):
        resolve_fs_effect(effect_of(observe_docx_target), {}, {})
