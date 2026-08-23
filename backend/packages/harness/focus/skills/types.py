"""
本文件对外提供 skill 数据模型定义。

对外提供:
    SKILL_MD_FILE — skill 主入口文件名常量
    SKILLS_PUBLIC_REAL_ROOT — public skills 宿主机存储路径 "skills"
    SKILLS_CUSTOM_REAL_ROOT — custom skills 宿主机存储路径模板 ".focus/users/{user_id}/skills"
    SkillCategory(StrEnum) — skill 类别枚举: PUBLIC(平台内置,只读) / CUSTOM(用户创建,可改)
    Skill — skill 数据类，持有从 SKILL.md frontmatter 解析出的元数据及文件系统位置信息

输入: 无 — 本文件为纯定义文件
输出: SKILL_MD_FILE 常量、SkillCategory 枚举、Skill 数据类

具体工作流:
    Skill 为 dataclass 实例，由 parser.py 中的 parse_skill_file 函数构造。
    SkillCategory 继承 StrEnum，可与字符串直接比较。

示例:
    from focus.skills.types import Skill, SkillCategory, SKILL_MD_FILE

    skill = Skill(
        name="pdf",
        description="PDF 文档处理",
        skill_dir=Path("/skills/public/pdf"),
        skill_file=Path("/skills/public/pdf/SKILL.md"),
        relative_path=Path("pdf"),
        category=SkillCategory.PUBLIC,
        allowed_tools=["read_file_tool", "write_file_tool"],
        enabled=True,
    )
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

SKILL_MD_FILE = "SKILL.md"
SKILLS_PUBLIC_REAL_ROOT = "skills"
SKILLS_CUSTOM_REAL_ROOT = ".focus/users/{user_id}/skills"


def skills_custom_root(user_id: str) -> "Path":
    """返回 per-user custom skills 宿主机路径（全局态 `~/.focus/users/<user_id>/skills`）。"""
    from focus.config.layered import global_home

    return global_home() / "users" / user_id / "skills"


class SkillCategory(StrEnum):
    PUBLIC = "public"    # 平台内置,只读
    CUSTOM = "custom"    # 用户创建,可编辑/删除


@dataclass
class Skill:
    name: str                           # 文件夹名 / 唯一标识(全局唯一)
    description: str                    # 一句话描述,从 SKILL.md frontmatter 解析
    license: str | None = None          # 可选许可证字符串
    skill_dir: Path = field(default_factory=Path)   # skill 文件夹的绝对路径
    skill_file: Path = field(default_factory=Path)  # SKILL.md 的绝对路径
    relative_path: Path = field(default_factory=Path)  # 从类别根目录算的相对路径(支持嵌套子目录)
    category: SkillCategory = SkillCategory.PUBLIC  # public / custom
    allowed_tools: list[str] | None = None  # 工具白名单: None=不限制, [] =禁用所有
    enabled: bool = False               # 是否启用(磁盘上有不代表用)
