import json
from pathlib import Path

import pytest

from backend.app.desktop.capabilities.skills import build_task_skill_catalog, describe_task_skill_catalog, resolve_task_skills


def _skill(root: Path, folder: str, name: str, description: str) -> None:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nInstructions for {description}.\n",
        encoding="utf-8",
    )


def test_catalog_merges_valid_enabled_skills_with_workspace_precedence(tmp_path):
    public = tmp_path / "public"
    workspace = tmp_path / "workspace"
    workspace_skills = workspace / ".agents" / "skills"
    config = tmp_path / "extensions_config.json"

    _skill(public, "z-copy", "shared", "public duplicate ignored")
    _skill(public, "a-copy", "shared", "public first path")
    _skill(public, "beta", "beta", "public beta")
    _skill(public, "disabled", "disabled", "not enabled")
    _skill(workspace_skills, "shared", "shared", "workspace wins")
    _skill(workspace_skills, "alpha", "alpha", "workspace alpha")
    invalid = workspace_skills / "invalid" / "SKILL.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("not frontmatter", encoding="utf-8")
    config.write_text(
        json.dumps({"skills": {name: {"enabled": True} for name in ("shared", "beta")}}),
        encoding="utf-8",
    )

    catalog = build_task_skill_catalog(
        workspace, public_root=public, extensions_path=config
    )

    assert list(catalog) == ["alpha", "beta", "shared"]
    assert catalog["shared"].description == "workspace wins"
    assert "disabled" not in catalog


def test_catalog_resolution_preserves_order_deduplicates_and_reports_stale(tmp_path):
    workspace = tmp_path / "workspace"
    _skill(workspace / ".agents" / "skills", "one", "one", "first")
    _skill(workspace / ".agents" / "skills", "two", "two", "second")
    catalog = build_task_skill_catalog(
        workspace,
        public_root=tmp_path / "missing",
        extensions_path=tmp_path / "missing.json",
    )

    snapshots, unavailable = resolve_task_skills(catalog, ["two", "missing", "one", "two"])

    assert [snapshot["name"] for snapshot in snapshots] == ["two", "one"]
    assert snapshots[0]["content"].endswith("Instructions for second.\n")
    assert unavailable == ["missing"]


def test_global_skills_are_shared_and_workspace_overrides_stay_local(tmp_path, monkeypatch):
    home = tmp_path / ".focus"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    _skill(home / ".agents" / "skills", "shared", "shared", "global")
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a / ".agents" / "skills", "shared", "shared", "only A")
    assert build_task_skill_catalog(a)["shared"].description == "only A"
    assert build_task_skill_catalog(b)["shared"].description == "global"
    from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool

    catalog = build_task_skill_catalog(a)
    result = build_describe_skill_tool(describe_task_skill_catalog(catalog)).invoke({"name": "shared"})
    assert f"Location: {a / '.agents' / 'skills' / 'shared' / 'SKILL.md'}" in result
    snapshots, _ = resolve_task_skills(catalog, ["shared"])
    assert snapshots[0]["content"] == catalog["shared"].metadata.skill_file.read_text(encoding="utf-8")


def test_bundled_migration_skill_is_available_outside_focus_checkout(tmp_path, monkeypatch):
    from focus.skills.types import SkillCategory
    from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool

    home = tmp_path / "global"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for workspace in (home, tmp_path / "project-a", tmp_path / "project-b"):
        catalog = build_task_skill_catalog(workspace)
        skill = catalog["focus-skill-to-strategy"]
        assert skill.metadata.category == SkillCategory.PUBLIC
        location = skill.metadata.skill_file
        assert location.is_file() and not location.is_relative_to(tmp_path)
        described = build_describe_skill_tool(describe_task_skill_catalog(catalog)).invoke(
            {"name": "focus-skill-to-strategy"}
        )
        assert f"Location: {location}" in described
        snapshots, unavailable = resolve_task_skills(catalog, ["focus-skill-to-strategy"])
        assert unavailable == []
        assert snapshots == [{"name": skill.name, "content": location.read_text(encoding="utf-8")}]


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_bundled_migration_skill_can_be_disabled(tmp_path, monkeypatch, scope):
    home, workspace = tmp_path / "global", tmp_path / "project"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    assert "focus-skill-to-strategy" in build_task_skill_catalog(workspace)
    config = (home if scope == "global" else workspace / ".focus") / "extensions_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"skills": {"focus-skill-to-strategy": {"enabled": False}}}), encoding="utf-8")

    catalog = build_task_skill_catalog(workspace)
    assert "focus-skill-to-strategy" not in catalog
    snapshots, unavailable = resolve_task_skills(catalog, ["focus-skill-to-strategy"])
    assert snapshots == [] and unavailable == ["focus-skill-to-strategy"]
    if scope == "workspace":
        assert "focus-skill-to-strategy" in build_task_skill_catalog(tmp_path / "other")


def test_custom_migration_skill_overrides_bundle_only_in_its_scope(tmp_path, monkeypatch):
    home, workspace = tmp_path / "global", tmp_path / "project"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    name = "focus-skill-to-strategy"
    assert name in build_task_skill_catalog(workspace)
    _skill(home / ".agents" / "skills", name, name, "global migration method")
    _skill(workspace / ".agents" / "skills", name, name, "workspace migration method")

    assert build_task_skill_catalog(workspace)[name].description == "workspace migration method"
    assert build_task_skill_catalog(tmp_path / "other")[name].description == "global migration method"


def test_bundled_migration_template_is_importable_by_focus(tmp_path, monkeypatch):
    from backend.app.desktop.capabilities.strategy_packages import parse_strategy

    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "global"))
    content = build_task_skill_catalog(tmp_path / "project")["focus-skill-to-strategy"].content
    template = content.split("```markdown\n", 1)[1].split("```", 1)[0]
    package = parse_strategy(template)
    assert package.metadata.id == "your-namespace/your-method"
    assert package.metadata.schema_version == "focus.strategy/v1"
    assert set(package.strategy.model_dump()) == {"applicability", "evolution", "inheritance"}
