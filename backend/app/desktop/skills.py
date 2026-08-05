from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from focus.skills.parser import parse_skill_file
from focus.skills.types import SKILL_MD_FILE, SkillCategory


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DesktopSkill:
    name: str
    description: str
    content: str


def _enabled_public_names(config_path: Path) -> frozenset[str]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
        result[skill.name] = DesktopSkill(skill.name, skill.description, content)
    return result


def build_task_skill_catalog(
    workspace: str | Path,
    *,
    public_root: Path | None = None,
    extensions_path: Path | None = None,
) -> dict[str, DesktopSkill]:
    public_root = public_root or ROOT / "skills" / "public"
    extensions_path = extensions_path or ROOT / "extensions_config.json"
    public = _scan(public_root, SkillCategory.PUBLIC, _enabled_public_names(extensions_path))
    workspace_skills = _scan(Path(workspace) / ".agents" / "skills", SkillCategory.CUSTOM)
    merged = {**public, **workspace_skills}
    return dict(sorted(merged.items(), key=lambda item: item[0].casefold()))


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
