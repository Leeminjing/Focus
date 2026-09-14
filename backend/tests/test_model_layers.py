"""模型目录两层合并语义的单测。

输入为文件层与用户偏好层的原始 map；输出为合并后的模型目录。用例锁定按条目名覆盖、
删除名单、单例标记归属三条语义，以及「未声明模型目录仍然显式失败」这一边界。
"""

from focus.config.model_layers import merge_model_layers


def _entry(name: str, **overrides) -> dict:
    entry = {
        "name": name,
        "display_name": name.upper(),
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "model": name,
        "api_key": "$OPENAI_API_KEY",
        "base_url": "https://api.example.com",
    }
    entry.update(overrides)
    return entry


def _names(merged: dict) -> list[str]:
    return [entry["name"] for entry in merged["models"]]


def test_overlay_entry_replaces_same_name_in_place():
    file_map = {"models": [_entry("a"), _entry("b")]}
    preference_map = {"models": [_entry("b", context_window=4096)]}

    merged = merge_model_layers(file_map, preference_map)

    assert _names(merged) == ["a", "b"]
    assert merged["models"][1]["context_window"] == 4096


def test_entries_not_declared_by_preference_layer_survive():
    file_map = {"models": [_entry("a"), _entry("b"), _entry("c")]}
    preference_map = {"models": [_entry("b", context_window=4096)]}

    merged = merge_model_layers(file_map, preference_map)

    assert _names(merged) == ["a", "b", "c"]
    assert merged["models"][0]["name"] == "a"


def test_preference_layer_adds_new_entry_at_the_end():
    file_map = {"models": [_entry("a")]}
    preference_map = {"models": [_entry("extra")]}

    merged = merge_model_layers(file_map, preference_map)

    assert _names(merged) == ["a", "extra"]


def test_adding_entry_leaves_other_entries_untouched():
    file_map = {"models": [_entry("a", context_window=8192)]}
    preference_map = {"models": [_entry("extra")]}

    merged = merge_model_layers(file_map, preference_map)

    assert merged["models"][0]["context_window"] == 8192


def test_removed_names_drop_entries():
    file_map = {"models": [_entry("a"), _entry("b")]}
    preference_map = {"models": [], "removed_models": ["b"]}

    merged = merge_model_layers(file_map, preference_map)

    assert _names(merged) == ["a"]
    assert merged["removed_models"] == ["b"]


def test_dangling_removed_name_is_ignored():
    file_map = {"models": [_entry("a")]}
    preference_map = {"removed_models": ["gone"]}

    merged = merge_model_layers(file_map, preference_map)

    assert _names(merged) == ["a"]
    assert merged["removed_models"] == ["gone"]


def test_removed_names_are_deduped_and_trimmed():
    preference_map = {"models": [], "removed_models": [" b ", "b", "", 7]}

    merged = merge_model_layers({"models": [_entry("a"), _entry("b")]}, preference_map)

    assert merged["removed_models"] == ["b"]
    assert _names(merged) == ["a"]


def test_both_layers_empty_does_not_inject_models_key():
    merged = merge_model_layers({"commitment": {"enabled": True}}, {})

    assert "models" not in merged
    assert "removed_models" not in merged
    assert merged["commitment"]["enabled"] is True


def test_declared_models_key_produces_merged_catalog():
    merged = merge_model_layers({"models": []}, {"models": [_entry("only")]})

    assert _names(merged) == ["only"]


def test_preference_default_flag_takes_the_singleton():
    file_map = {"models": [_entry("a", default=True), _entry("b")]}
    preference_map = {"models": [_entry("b", default=True)]}

    merged = merge_model_layers(file_map, preference_map)

    assert [e["name"] for e in merged["models"] if e.get("default")] == ["b"]


def test_file_layer_default_survives_when_preference_layer_is_silent():
    file_map = {"models": [_entry("a", default=True), _entry("b")]}
    preference_map = {"models": [_entry("b", context_window=4096)]}

    merged = merge_model_layers(file_map, preference_map)

    assert [e["name"] for e in merged["models"] if e.get("default")] == ["a"]


def test_neither_layer_declares_default():
    merged = merge_model_layers({"models": [_entry("a")]}, {"models": [_entry("b")]})

    assert [e["name"] for e in merged["models"] if e.get("default")] == []


def test_curation_default_follows_the_same_singleton_rule():
    file_map = {"models": [_entry("a", curation_default=True, curation_output_method="prompt_json")]}
    preference_map = {
        "models": [_entry("b", curation_default=True, curation_output_method="json_mode")]
    }

    merged = merge_model_layers(file_map, preference_map)

    curated = [e["name"] for e in merged["models"] if e.get("curation_default")]
    assert curated == ["b"]


def test_preference_entry_without_name_is_kept_for_validation_to_reject():
    preference_map = {"models": [{"display_name": "broken"}]}

    merged = merge_model_layers({"models": [_entry("a")]}, preference_map)

    assert [e["name"] for e in merged["models"] if "name" in e] == ["a"]
    assert {"display_name": "broken"} in merged["models"]
