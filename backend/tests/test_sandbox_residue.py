"""沙箱时代残骸的结构守卫与技能位置的宿主可解析性用例。

输入为宿主上的技能文件、执行状态声明与相关源码；输出为技能位置与三处残骸的存在性结论。
工作流先验证技能位置在宿主上可解析，再逐处断言虚拟挂载前缀、空隔离标识槽位与
手工拼装执行上下文的残留都不存在。
"""

from pathlib import Path

from focus.agents import lead_agent_state
from focus.skills.catalog import SkillCatalog
from focus.skills.types import Skill, SkillCategory
from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool

_ROOT = Path(__file__).parents[1]
_HARNESS = _ROOT / "packages" / "harness" / "focus"


def _sources() -> list[tuple[Path, str]]:
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(_HARNESS.rglob("*.py"))
    ]


def test_skill_location_is_a_real_host_path(tmp_path):
    """技能位置必须是宿主上真实存在的路径，模型据此直接读取，无需翻译虚拟前缀。"""
    skill_dir = tmp_path / "skills" / "public" / "pdf"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# pdf\n", encoding="utf-8")
    catalog = SkillCatalog(
        [
            Skill(
                name="pdf",
                description="PDF 工具",
                skill_dir=skill_dir,
                skill_file=skill_file,
                relative_path=Path("pdf"),
                category=SkillCategory.PUBLIC,
            )
        ]
    )

    result = build_describe_skill_tool(catalog).invoke({"name": "pdf"})

    location = next(
        line.removeprefix("Location: ")
        for line in result.splitlines()
        if line.startswith("Location: ")
    )
    assert Path(location).is_file()
    assert Path(location) == skill_file


def test_no_virtual_mount_prefix_remains():
    """不再存在任何虚拟挂载前缀：技能位置不返回沙箱内的挂载路径。"""
    offenders = [
        str(path)
        for path, source in _sources()
        if "/mnt/" in source or "_SKILL_VROOT" in source
    ]
    assert offenders == []


def test_execution_state_has_no_isolation_slot():
    """本地 Agent 不存在隔离标识，执行状态里不应再留空的隔离状态槽位。"""
    assert not hasattr(lead_agent_state, "SandboxState")
    assert "sandbox" not in lead_agent_state.LeadAgentState.__annotations__


def test_commitment_subgraph_derives_its_own_context():
    """承诺子图是派生执行：身份由父级安全上下文单调派生，不从扁平上下文搬运受治理字段。"""
    source = (
        _HARNESS / "agents" / "commitment" / "middleware.py"
    ).read_text(encoding="utf-8")
    assert "derive_child_security_context(" in source
    assert "ChildRole.COMMITMENT_WORKER" in source
    assert "context=child_security.to_runtime_context()" in source
    assert 'runtime_context.get("workspace"' not in source


def test_derived_tools_do_not_hand_assemble_context():
    """派生工具同样只做单调派生，不出现手工拼装的受治理字段。"""
    source = (
        _HARNESS / "tools" / "builtins" / "spawn_agent_tool.py"
    ).read_text(encoding="utf-8")
    assert "security_context_of(runtime.context)" in source
    assert "derive_child_security_context(" in source
    assert "context=child_security.to_runtime_context()" in source
    assert '"permissions"' not in source
    assert '"workspace"' not in source
