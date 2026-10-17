from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from focus.config.layered import global_home
from focus.skills.catalog import SkillCatalog
from focus.skills.parser import parse_skill_file
from focus.skills.types import SKILL_MD_FILE, Skill, SkillCategory


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DesktopSkill:
    name: str
    description: str
    content: str
    metadata: Skill


def _enabled_public_names(config_path: Path, workspace: str | Path) -> frozenset[str]:
    try:
        from focus.config.layered import load_layered_map

        raw = load_layered_map(config_path.name, str(config_path), workspace=workspace)
    except Exception:
        return frozenset()
    skills = raw.get("skills")
    if not isinstance(skills, dict):
        return frozenset()
    return frozenset(
        name
        for name, config in skills.items()
        if isinstance(config, dict) and config.get("enabled") is True
    )


def _scan(root: Path, category: SkillCategory, enabled: frozenset[str] | None = None) -> dict[str, DesktopSkill]:
    if not root.is_dir():
        return {}
    resolved_root = root.resolve()
    result: dict[str, DesktopSkill] = {}
    for skill_file in sorted(root.rglob(SKILL_MD_FILE), key=lambda path: path.as_posix().casefold()):
        try:
            resolved_file = skill_file.resolve()
            resolved_file.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        skill = parse_skill_file(resolved_file, category, resolved_file.parent.relative_to(resolved_root))
        if skill is None or enabled is not None and skill.name not in enabled or skill.name in result:
            continue
        try:
            content = resolved_file.read_text(encoding="utf-8")
        except OSError:
            continue
        result[skill.name] = DesktopSkill(skill.name, skill.description, content, skill)
    return result


def build_task_skill_catalog(
    workspace: str | Path,
    *,
    public_root: Path | None = None,
    extensions_path: Path | None = None,
) -> dict[str, DesktopSkill]:
    public_root = public_root or ROOT / "skills" / "public"
    extensions_path = extensions_path or ROOT / "extensions_config.json"
    public = _scan(public_root, SkillCategory.PUBLIC, _enabled_public_names(extensions_path, workspace))
    global_skills = _scan(global_home() / ".agents" / "skills", SkillCategory.CUSTOM)
    workspace_skills = _scan(Path(workspace) / ".agents" / "skills", SkillCategory.CUSTOM)
    merged = {**public, **global_skills, **workspace_skills}
    return dict(sorted(merged.items(), key=lambda item: item[0].casefold()))


def describe_task_skill_catalog(catalog: dict[str, DesktopSkill]) -> SkillCatalog:
    return SkillCatalog(skill.metadata for skill in catalog.values())


def resolve_task_skills(
    catalog: dict[str, DesktopSkill], selected_names: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    snapshots: list[dict[str, str]] = []
    unavailable: list[str] = []
    seen: set[str] = set()
    for name in selected_names:
        if name in seen:
            continue
        seen.add(name)
        skill = catalog.get(name)
        if skill is None:
            unavailable.append(name)
        else:
            snapshots.append({"name": skill.name, "content": skill.content})
    return snapshots, unavailable
