import json
from pathlib import Path

from backend.app.desktop.skills import build_task_skill_catalog, describe_task_skill_catalog, resolve_task_skills


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
