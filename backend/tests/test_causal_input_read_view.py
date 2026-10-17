"""本文件验证因果变化视图按身份去重与版本绑定。

输入为冻结状态和可见引用；输出为有界变化、明确省略及原快照不变的断言。
具体工作流为调用实际投影和分页合同，验证 UTF-8、哈希与旧版本值。
示例：python -m pytest backend/tests/test_causal_input_read_view.py -q。
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.app.desktop.agent_loop.causal_read_view import CausalInputReadView
from backend.app.desktop.agent_loop.patrol_read_view import PatrolCausalReadRequest, PatrolReadView
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash


def test_changed_fact_only_carries_changed_fields_and_original_revision():
    before = {"shared": "相同内容" * 5000, "intervention": {"facts": [
        {"fact_id": "fact", "revision": 1, "state": "observed", "presentation": {"summary": "old"}}]},
        "user_intents": [{"content": "U1"}]}
    after = deepcopy(before)
    after["intervention"]["facts"][0].update(revision=2, state="verified")
    after["user_intents"].append({"content": "U2"})
    original = deepcopy(after)
    source = {"intent_id": "U1", "observation_id": "O1", "generation": 1, "snapshot_hash": canonical_hash(before)}
    view = CausalInputReadView.build(after, previous=before, previous_source=source)
    assert view["previous"] == source
    assert view["snapshot_hash"] == canonical_hash(after)
    assert view["complete"] and view["omitted_changes"] == 0
    assert view["changes"] == [
        {"path": ["intervention", "facts", {"fact_id": "fact"}, "revision"], "before": 1, "after": 2},
        {"path": ["intervention", "facts", {"fact_id": "fact"}, "state"], "before": "observed", "after": "verified"}]
    assert len(PatrolReadView._encoded(view)) < len(PatrolReadView._encoded(after)) / 20
    assert after == original


def test_additions_deletions_and_explicit_null_are_distinct():
    view = CausalInputReadView.build({"a": None, "new": "value"}, previous={"a": "old", "gone": None})
    assert view["changes"] == [{"path": ["a"], "before": "old", "after": None},
                               {"path": ["gone"], "before": None}, {"path": ["new"], "after": "value"}]


def test_entity_order_change_does_not_repeat_unchanged_bodies():
    rows = [{"run_id": "a", "text": "large unchanged content" * 1000}, {"run_id": "b"}]
    view = CausalInputReadView.build({"runs": list(reversed(rows))}, previous={"runs": rows})
    assert view["changes"] == [{"path": ["runs"], "identity_order": "run_id",
                                "before": ["a", "b"], "after": ["b", "a"]}]


def test_json_scalar_type_changes_are_preserved_inside_collections():
    view = CausalInputReadView.build({"values": [True, None]}, previous={"values": [1, "old"]})
    assert len(view["changes"]) == 2
    assert type(view["changes"][0]["before"]) is int
    assert type(view["changes"][0]["after"]) is bool


def test_large_changes_and_first_input_are_explicitly_incomplete():
    after = {"large": "new" * 20000, "small": 2}
    view = CausalInputReadView.build(after, previous={"large": "old" * 20000, "small": 1})
    assert view["omitted_changes"] == 1 and not view["complete"]
    assert view["changes"] == [{"path": ["small"], "before": 1, "after": 2}]
    baseline = CausalInputReadView.build(after)
    assert baseline["comparison"] == "baseline_only" and not baseline["complete"]
    assert baseline["changes"] == [] and baseline["snapshot_hash"] == canonical_hash(after)


def test_visible_previews_are_frozen_once_and_catalog_rejects_wrong_binding():
    visible = {"status": "declared", "run_previews": [{"text": "用户当时看到的内容", "gap": True}]}
    snapshot = {"intervention": {"visible_context": visible}}
    view = CausalInputReadView.build(snapshot, previous={"intervention": {"visible_context": {}}})
    visible["run_previews"][0]["text"] = "later"
    assert view["visible_context"]["run_previews"][0]["text"] == "用户当时看到的内容"
    assert view["changes"] == []
    with pytest.raises(ValueError, match="hash"):
        PatrolReadView.causal_catalog(SimpleNamespace(causal_chain=[{
            "intent_id": "input", "snapshot_hash": "a" * 64, "read_view": view}]))


def test_causal_pages_reject_changed_content_and_invalid_utf8_cursor():
    snapshot = {"value": "事实" * 100}
    request = PatrolCausalReadRequest(intent_id="input", snapshot_hash=canonical_hash(snapshot), max_bytes=256)
    with pytest.raises(ValueError, match="hash"):
        PatrolReadView.causal_page({"value": "later"}, request)
    first = PatrolReadView.causal_page(snapshot, request)
    assert first["next_cursor"] == len(first["content"].encode("utf-8"))
    start = PatrolReadView._encoded(snapshot).index("事".encode("utf-8"))
    with pytest.raises(ValueError, match="UTF-8"):
        PatrolReadView.causal_page(snapshot, request.model_copy(update={"cursor": start + 1}))
