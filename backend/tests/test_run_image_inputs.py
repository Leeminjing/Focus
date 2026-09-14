"""本文件验证 run 图片值对象与最终请求预算。

输入为附件描述、必看 ID、旧/新 equipment 和消息；输出为顺序去重、子集拒绝、JSON 往返、
旧数据兼容、上下文投影和 request-only 图片计量断言。具体工作流不访问数据库或文件系统。

示例：python -m pytest backend/tests/test_run_image_inputs.py。
"""

import pytest
from langchain_core.messages import HumanMessage

from focus.agents.image_inputs import (
    MODEL_IMAGE_INPUT_KEY,
    RUN_IMAGE_INPUTS_CONTEXT_KEY,
    RunImageInputs,
    project_run_image_context,
)
from focus.messages import estimate_messages_tokens, estimate_model_request_tokens


def material(material_id: str, tokens: int = 10, size: int = 100) -> dict:
    return {
        "material_id": material_id,
        "relative_path": f"{material_id}.png",
        "model_bytes": size,
        "model_tokens": tokens,
    }


def test_build_deduplicates_in_order_and_aggregates() -> None:
    inputs = RunImageInputs.build([material("m2"), material("m1"), material("m2")], ["m1"])
    assert [item.material_id for item in inputs.attached] == ["m2", "m1"]
    assert inputs.required_ids == ("m1",)
    assert inputs.total_model_tokens == 20
    assert inputs.total_model_bytes == 200


def test_required_must_be_attached() -> None:
    with pytest.raises(ValueError, match="必须先附加"):
        RunImageInputs.build([material("m1")], ["m2"])


def test_empty_and_json_round_trip() -> None:
    assert RunImageInputs.from_json(None) == RunImageInputs.empty()
    original = RunImageInputs.build([material("m1", 22, 44)], ["m1"])
    assert RunImageInputs.from_json(original.to_json()) == original


def test_legacy_equipment_normalizes_attached_equal_required() -> None:
    inputs = RunImageInputs.from_equipment({"must_view_materials": [material("m1")]})
    assert [item.material_id for item in inputs.attached] == ["m1"]
    assert inputs.required_ids == ("m1",)
    assert "must_view_materials" not in inputs.to_equipment()


def test_context_projection_is_complete() -> None:
    inputs = RunImageInputs.build([material("m1")], [])
    context = project_run_image_context({"workspace": "w"}, inputs, True)
    assert context[RUN_IMAGE_INPUTS_CONTEXT_KEY] == inputs.to_json()
    assert context[MODEL_IMAGE_INPUT_KEY] is True


def test_request_only_images_add_tokens_without_changing_plain_text() -> None:
    messages = [{"role": "human", "content": "hello"}]
    baseline = estimate_messages_tokens(messages)
    assert estimate_model_request_tokens(messages) == baseline
    inputs = RunImageInputs.build([material("m1", tokens=77)], [])
    assert estimate_model_request_tokens(messages, inputs.attached) == baseline + 77


def test_request_image_identity_is_not_counted_twice() -> None:
    messages = [{
        "role": "human",
        "content": [{
            "type": "image_url",
            "material_id": "m1",
            "image_url": {"url": "data:image/png;base64,bm90LWltYWdl"},
        }],
    }]
    inputs = RunImageInputs.build([material("m1", tokens=77)], [])
    assert estimate_model_request_tokens(messages, inputs.attached) == estimate_messages_tokens(messages)


def test_compression_gate_uses_request_only_image_budget(monkeypatch) -> None:
    import focus.agents.compression.gate as compression

    seen = {}
    monkeypatch.setattr(
        compression,
        "interrupt",
        lambda payload: seen.update(payload) or {"type": "compression", "decision": "cancel"},
    )
    inputs = RunImageInputs.build([material("m1", tokens=77)], [])
    runtime = type("Runtime", (), {"context": inputs.to_equipment()})()
    state = {"messages": [HumanMessage(content="hello")]}
    baseline = estimate_model_request_tokens(state["messages"])
    gate = compression.CompressionGate(context_window=baseline + 77, threshold_ratio=0.75)
    assert baseline < gate._limit
    gate.before_model(state, runtime)
    assert seen["type"] == "compression_request"
    assert seen["usage"] == baseline + 77
