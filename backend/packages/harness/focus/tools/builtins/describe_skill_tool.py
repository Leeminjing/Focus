"""
本文件对外提供 build_describe_skill_tool 工厂函数，创建 describe_skill 工具供 agent 按需查询 skill 元数据。

对外提供:
    build_describe_skill_tool — 工厂函数，接收 SkillCatalog，返回 StructuredTool 实例

输入:
    catalog: SkillCatalog — 已启用的 skill 检索索引

输出:
    StructuredTool — 工具名 "describe_skill", 接受 name: str 参数；
    效果契约为「无受治理本地效果」（只读检索内存索引，不读写工作区）

具体工作流:
    describe_skill(name):
    (1) 调用 catalog.search(name) 查找匹配的 Skill
    (2) 精确匹配(name 相等) → 返回 description / allowed_tools / SKILL.md 的真实宿主路径
    (3) 模糊匹配 → 返回可能的 skill 名称列表，提示 agent 精确指定
    (4) 无匹配 → 返回错误信息，提示检查 <skill_index>

    build_describe_skill_tool:
    (1) 定义内部函数 describe_skill(name: str) 通过闭包捕获 catalog
    (2) 调用 StructuredTool.from_function() 构造 LangChain Tool

示例:
    from focus.skills.catalog import SkillCatalog
    from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool

    catalog = SkillCatalog([...])
    tool = build_describe_skill_tool(catalog)
    result = tool.invoke({"name": "pdf"})
"""

import logging

from langchain_core.tools import StructuredTool

from focus.security.effects import NO_LOCAL_EFFECT, declare_effect
from focus.skills.catalog import SkillCatalog

logger = logging.getLogger(__name__)


def build_describe_skill_tool(catalog: SkillCatalog) -> StructuredTool:
    def describe_skill(name: str) -> str:
        """Look up a skill by name and return its description, allowed tools, and file location.

        Use this tool to discover whether a skill is available before loading its full instructions.
        This enables lazy skill discovery — you see skill names in <skill_index>, call this tool
        to check if one matches your task, then read_file() on the returned location if it does.

        When to use the describe_skill tool:
        - When you see a skill name in <skill_index> that might match your current task
        - When you want to check what a skill does before loading its full SKILL.md
        - When you need to know which tools a skill allows before activating it

        When NOT to use the describe_skill tool:
        - When you already know the skill is relevant — use read_file directly on SKILL.md
        - When you need the full skill instructions — this only returns metadata, not the full content

        Args:
            name: The skill name to look up (must match a name from <skill_index> exactly)
        """
        if not name or not name.strip():
            return (
                "Please specify a skill name. "
                "Available skills: " + ", ".join(sorted(catalog.names))
            )

        results = catalog.search(name.strip())

        if not results:
            return (
                f"Skill '{name}' not found. "
                "Check <skill_index> for available skill names. "
                "Available: " + ", ".join(sorted(catalog.names))
            )

        exact = [s for s in results if s.name.lower() == name.strip().lower()]
        if exact:
            s = exact[0]
            location = str(s.skill_file)
            parts = [
                f"Skill: {s.name}",
                f"Description: {s.description}",
                f"Allowed tools: {s.allowed_tools if s.allowed_tools is not None else 'unrestricted'}",
                f"Location: {location}",
            ]
            if s.license:
                parts.append(f"License: {s.license}")
            return "\n".join(parts)

        names = [s.name for s in results]
        return (
            f"Multiple skills match '{name}': {', '.join(names)}. "
            "Please specify the exact skill name."
        )

    return declare_effect(
        StructuredTool.from_function(
            func=describe_skill,
            name="describe_skill",
            description=describe_skill.__doc__,
        ),
        NO_LOCAL_EFFECT,
    )
