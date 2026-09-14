"""权柄面派生、优先于工作根与读写分离的用例。

输入为工作区、真实目标路径与访问策略；输出为权柄面归属与准入判定结果。
工作流先锁定权柄面的成员由判定原则派生（含「加载器会去看的候选路径」且普通说明文件不算），
再锁定权柄面优先于工作根、读写分别判定，最后锁定访问模式与能力权限正交且缺省最严。
"""

import logging
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime

from focus.config.layered import global_home
from focus.security.authority import (
    AuthorityKind,
    AuthoritySurface,
    authority_surface_for,
    authority_surfaces,
)
from focus.security.policy import (
    AccessDecision,
    AccessMode,
    AccessOperation,
    AccessPolicy,
    decide_path_access,
    policy_from_context,
    restrictive_policy,
)


def _workspace_policy(tmp_path) -> AccessPolicy:
    return restrictive_policy(tmp_path)


def test_surfaces_cover_credentials_configs_and_executable_roots(tmp_path):
    surfaces = authority_surfaces(tmp_path)
    paths = {surface.path for surface in surfaces}
    home = global_home()

    assert (home / ".env") in paths
    # 分层配置在两层各有一个物理候选，且即使尚不存在也必须覆盖
    assert (home / "config.yaml") in paths
    assert Path("config.yaml").resolve() in paths
    assert (home / "extensions_config.json") in paths
    assert Path("extensions_config.json").resolve() in paths
    assert (home / "plugins-disabled.json") in paths
    assert (home / "tools") in paths
    assert (home / "plugins") in paths
    assert (home / "users") in paths
    assert (tmp_path / ".agents" / "skills") in paths


def test_surface_kinds_mark_credentials_and_executable_roots(tmp_path):
    by_path = {surface.path: surface for surface in authority_surfaces(tmp_path)}
    assert by_path[global_home() / ".env"].kind is AuthorityKind.CREDENTIALS
    assert by_path[global_home() / "tools"].kind is AuthorityKind.TOOL_SET
    assert by_path[global_home() / "plugins"].kind is AuthorityKind.CODE
    assert by_path[global_home() / "plugins-disabled.json"].kind is AuthorityKind.TRUST


def test_credentials_are_not_readable_but_plain_configs_are(tmp_path):
    by_path = {surface.path: surface for surface in authority_surfaces(tmp_path)}
    assert by_path[global_home() / ".env"].readable is False
    assert by_path[Path("config.yaml").resolve()].readable is True


def test_ordinary_workspace_files_are_not_authority_surfaces(tmp_path):
    """限定词是「无需模型请求即自动进入未来执行」：普通说明文件只是工作内容。"""
    surfaces = authority_surfaces(tmp_path)
    assert authority_surface_for(tmp_path / "README.md", surfaces) is None
    assert authority_surface_for(tmp_path / "src" / "main.py", surfaces) is None


def test_workspace_skill_directory_is_an_authority_surface(tmp_path):
    surfaces = authority_surfaces(tmp_path)
    surface = authority_surface_for(tmp_path / ".agents" / "skills" / "x" / "SKILL.md", surfaces)
    assert surface is not None
    assert surface.kind is AuthorityKind.INSTRUCTIONS


def test_authority_takes_precedence_over_the_workspace_root(tmp_path):
    """工作区就是应用仓库时，应用配置仍按权柄面处理，而同目录普通源文件直接放行。"""
    workspace_config = tmp_path / "config.yaml"
    policy = AccessPolicy(
        mode=AccessMode.WORKSPACE,
        workspace=tmp_path,
        roots=(tmp_path,),
        authority=(AuthoritySurface(AuthorityKind.INSTRUCTIONS, workspace_config),),
    )
    assert decide_path_access(policy, workspace_config, AccessOperation.WRITE) is AccessDecision.ASK
    assert (
        decide_path_access(policy, tmp_path / "src" / "main.py", AccessOperation.WRITE)
        is AccessDecision.ALLOW
    )


def test_read_and_write_are_decided_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "home"))
    policy = _workspace_policy(tmp_path / "ws")
    credential = global_home() / ".env"
    config = global_home() / "config.yaml"

    assert decide_path_access(policy, credential, AccessOperation.READ) is AccessDecision.ASK
    assert decide_path_access(policy, credential, AccessOperation.WRITE) is AccessDecision.ASK
    assert decide_path_access(policy, config, AccessOperation.READ) is AccessDecision.ALLOW
    assert decide_path_access(policy, config, AccessOperation.WRITE) is AccessDecision.ASK


def test_full_mode_lifts_the_local_resource_layer_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "home"))
    policy = AccessPolicy(
        mode=AccessMode.FULL, workspace=tmp_path / "ws", roots=(tmp_path / "ws",),
        authority=authority_surfaces(tmp_path / "ws"),
    )
    assert decide_path_access(policy, tmp_path / "outside.txt", AccessOperation.WRITE) is AccessDecision.ALLOW
    assert decide_path_access(policy, global_home() / ".env", AccessOperation.WRITE) is AccessDecision.ALLOW


def test_missing_mode_defaults_to_workspace(tmp_path):
    policy = policy_from_context({"workspace": str(tmp_path)})
    assert policy.mode is AccessMode.WORKSPACE


def test_unrecognised_mode_defaults_to_workspace_with_a_warning(tmp_path):
    """无法识别的模式按最严处理并留下可见告警；用例自带 handler，不依赖全局日志配置。"""
    import focus.security.policy as policy_module

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    policy_module.logger.addHandler(handler)
    try:
        policy = policy_from_context({"workspace": str(tmp_path), "access_mode": "bogus"})
    finally:
        policy_module.logger.removeHandler(handler)

    assert policy.mode is AccessMode.WORKSPACE
    assert any("访问模式" in record.getMessage() for record in records)


def _runtime(mode: str, workspace: Path, permissions: tuple[str, ...]) -> ToolRuntime:
    from focus.security.context import (
        AuthorizationIdentity,
        ExecutionProfile,
        RoutingIdentity,
        derive_security_context,
    )
    from focus.security.policy import workspace_roots

    return ToolRuntime(
        state={},
        context=derive_security_context(
            ExecutionProfile(
                authorization=AuthorizationIdentity(
                    workspace=workspace,
                    roots=workspace_roots(workspace),
                    permissions=permissions,
                    access_mode=AccessMode(mode),
                    agent_role="main",
                ),
                routing=RoutingIdentity("thread-1", "ws-1", "main:task-1", ""),
            )
        ).to_runtime_context(),
        config={}, stream_writer=None, tool_call_id=None, store=None, tools=[],
    )


def _asks(tmp_path, workspace: Path, permissions: tuple[str, ...], mode: str, name: str,
          args: dict[str, object]) -> bool:
    from focus.security.middleware import _admit
    from focus.tools.builtins.workspace_tools import select_workspace_tools
    from langchain.tools.tool_node import ToolCallRequest

    tools = select_workspace_tools(list(permissions))
    tool = next(tool for tool in tools if tool.name == name)
    return _admit(
        ToolCallRequest(
            tool_call={"name": name, "args": args, "id": "c1"},
            tool=tool, state={}, runtime=_runtime(mode, workspace, permissions),
        )
    ).asked


def test_access_mode_does_not_change_the_tool_set(tmp_path):
    """模式与能力权限正交：工具集合只由权限决定，同一调用的准入结论只由模式决定。"""
    import inspect

    from focus.tools.builtins.workspace_tools import select_workspace_tools

    # 工具集合是权限的函数：选择入口不接受模式，因此两档模式下同一权限集合得到的集合必然相同
    assert list(inspect.signature(select_workspace_tools).parameters) == ["permissions"]
    assert "powershell" not in _names(("read",))
    assert "powershell" in _names(("read", "write", "host_command"))

    # 四种组合的准入语义：命中允许面之外的路径时，只有工作区保护请求人工决定
    outside = str(tmp_path.parent / "outside.txt")
    combinations = [
        (permissions, mode)
        for permissions in (("read",), ("read", "write", "host_command"))
        for mode in ("workspace", "full")
    ]
    for permissions, mode in combinations:
        assert _asks(tmp_path, tmp_path, permissions, mode, "read_file", {"path": "notes.md"}) is False
        assert _asks(tmp_path, tmp_path, permissions, mode, "read_file", {"path": outside}) is (
            mode == "workspace"
        )
    for mode in ("workspace", "full"):
        assert _asks(
            tmp_path, tmp_path, ("read", "write", "host_command"), mode, "powershell", {"command": "ls"}
        ) is (mode == "workspace")


def _names(permissions: tuple[str, ...]) -> list[str]:
    from focus.tools.builtins.workspace_tools import select_workspace_tools

    return [tool.name for tool in select_workspace_tools(list(permissions))]


def test_full_mode_does_not_touch_the_other_hard_protocols(tmp_path):
    """完全权限只解除本机资源准入：其它硬协议不读访问模式，因此与模式无关。"""
    root = Path(__file__).parents[2]
    mechanisms = {
        "承诺层人工确认与子图": "backend/packages/harness/focus/agents/commitment/middleware.py",
        "承诺层九阶段规则": "backend/packages/harness/focus/agents/commitment/stage_rules.py",
        "承诺层委派闭环": "backend/packages/harness/focus/agents/commitment/delegation.py",
        "上下文投影": "backend/app/desktop/context_projection.py",
        "压缩范围校验": "backend/packages/harness/focus/agents/compression/schemas.py",
        "压缩门": "backend/packages/harness/focus/agents/compression/gate.py",
    }
    for label, relative in mechanisms.items():
        source = (root / relative).read_text(encoding="utf-8")
        assert "access_mode" not in source, label


def test_hard_protocol_mechanisms_remain_in_place():
    """完全权限不解除这些机制：它们各自的存在性由结构断言守住。"""
    root = Path(__file__).parents[2]
    desktop = (root / "backend/app/desktop/service.py").read_text(encoding="utf-8")
    projection = (root / "backend/app/desktop/context_projection.py").read_text(encoding="utf-8")

    # Patrol 的只读下限只看能力权限，与访问模式无关
    assert 'forbidden = set(equipment.get("permissions") or []) & {"write", "host_command"}' in desktop
    # 上下文投影的降级仍然只产出候选，由哈希绑定的接受/拒绝决定
    assert "approval_required" in projection
    assert "projection_hash" in projection
    assert "definition_hash" in projection
