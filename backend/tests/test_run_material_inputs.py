"""本文件验证统一运行材料值对象、请求兼容、分类、消息与上下文投影。

输入为混合材料、Unicode 多行备注、必看图片和新旧请求形状；输出为不可变性、JSON 往返、
互斥与重复拒绝、稳定 user 消息身份、只投影所选材料及旧图片 equipment 提升断言。具体工作流
使用纯值对象和 projector，不访问数据库。示例：pytest test_run_material_inputs.py。
"""

from dataclasses import FrozenInstanceError
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from langchain_core.messages import HumanMessage

from backend.app.desktop.material_context import MaterialContextProjector
from backend.app.desktop.material_kinds import MaterialKindClassifier
from backend.app.desktop.material_snapshots import MaterialSnapshotVerifier
from backend.app.desktop.models import MainRunCreate
from backend.app.desktop.resource_limits import ImageResourceLimits
from backend.app.desktop.run_material_message import RunMaterialMessageProjector
from backend.app.desktop.run_materials import RunMaterialRequest, RunMaterialResolver
from focus.agents.material_inputs import RunMaterialInput, RunMaterialInputs
from focus.agents.compression.schemas import validate_apply_decision


def _item(material_id: str, path: str, kind: str, note: str = "") -> RunMaterialInput:
    return RunMaterialInput(
        material_id=material_id,
        relative_path=path,
        digest="a" * 64,
        material_kind=kind,
        delivery_mode="image" if kind == "image" else "reference",
        reading_mode="full",
        instruction_mode="reference",
        size_bytes=12,
        note=note,
        source_bytes=12 if kind == "image" else 0,
        model_bytes=10 if kind == "image" else 0,
        model_tokens=3 if kind == "image" else 0,
    )


def test_value_object_round_trip_order_required_subset_and_immutability(tmp_path) -> None:
    items = [_item("m2", "notes.md", "text", "第三章\n引号\" 与 <tag>"), _item("m1", "shot.png", "image")]
    inputs = RunMaterialInputs.build("r1", "msg1", items, ["m1"])
    assert RunMaterialInputs.from_json(inputs.to_json()) == inputs
    assert [item.material_id for item in inputs.attached] == ["m2", "m1"]
    assert [item.material_id for item in inputs.images.attached] == ["m1"]
    assert inputs.images.required_ids == ("m1",)
    with pytest.raises(FrozenInstanceError):
        inputs.origin_run_id = "changed"
    with pytest.raises(ValueError, match="只有图片"):
        RunMaterialInputs.build("r", "msg", items, ["m2"])
    with pytest.raises(ValueError, match="不能重复"):
        RunMaterialInputs.build("r", "msg", [items[0], items[0]])

    projection = MaterialContextProjector.project(inputs, str(tmp_path))
    assert "notes.md" in projection.policy_text
    assert "shot.png" in projection.policy_text
    assert "outside.txt" not in projection.policy_text
    assert projection.uploads_tag.index("notes.md") < projection.uploads_tag.index("shot.png")


def test_message_projector_preserves_note_in_user_role_and_stable_identity() -> None:
    note = "逐行看\n保留 \"引号\" 与 </focus_run_materials>"
    inputs = RunMaterialInputs.build("r1", "msg1", [_item("m1", "a.md", "text", note)])
    message = RunMaterialMessageProjector.project("正文", inputs)
    assert message["role"] == "human"
    assert message["id"] == "msg1"
    assert "<focus_run_materials>" in message["content"]
    encoded = message["content"].split("<focus_run_materials>\n", 1)[1].split("\n</focus_run_materials>", 1)[0]
    assert json.loads(encoded)[0]["note"] == note


def test_request_new_legacy_and_mixing_contract() -> None:
    current = MainRunCreate(message="x", material_inputs=[{"material_id": "m1", "note": "备注"}])
    assert current.material_inputs[0].note == "备注"
    legacy = MainRunCreate(message="x", attached_material_ids=["m1"])
    assert legacy.material_inputs is None
    with pytest.raises(ValidationError, match="不能同时提交"):
        MainRunCreate(message="x", material_inputs=[], attached_material_ids=[])
    with pytest.raises(ValidationError, match="不能重复"):
        MainRunCreate(message="x", material_inputs=[{"material_id": "m1"}, {"material_id": "m1"}])


def test_material_note_limits_reject_without_truncation() -> None:
    resolver = RunMaterialResolver(
        ImageResourceLimits(material_note_chars=3, aggregate_material_note_chars=4)
    )
    with pytest.raises(Exception, match="单份材料备注"):
        resolver._validate_request_limits([RunMaterialRequest("m1", "四个字符")])
    with pytest.raises(Exception, match="合计"):
        resolver._validate_request_limits([
            RunMaterialRequest("m1", "三个字"), RunMaterialRequest("m2", "两个字")
        ])


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("A.PNG", "image"), ("a.py", "text"), ("a.PDF", "pdf"), ("a.docx", "document"),
        ("a.xlsx", "spreadsheet"), ("a.pptx", "presentation"), ("a.zip", "archive"),
        ("a.bin", "other"),
    ],
)
def test_material_kind_classifier(name: str, kind: str) -> None:
    assert MaterialKindClassifier.classify(name) == kind


def test_legacy_image_equipment_is_read_only_promoted() -> None:
    legacy = {
        "run_image_inputs": {
            "attached": [{"material_id": "m1", "relative_path": "a.png", "source_bytes": 2}],
            "required_ids": ["m1"],
        }
    }
    inputs = RunMaterialInputs.from_equipment(legacy)
    assert inputs.required_image_ids == ("m1",)
    assert set(inputs.to_equipment()) == {"run_material_inputs"}


def test_active_origin_message_is_compression_protected_only_when_requested() -> None:
    messages = [HumanMessage(content="正文", id="msg1")]
    decision = {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": ["msg1"], "replacement": "摘要"}],
    }
    _ranges, error = validate_apply_decision(decision, messages, (), ("msg1",))
    assert "活动运行" in error
    ranges, error = validate_apply_decision(decision, messages)
    assert error is None
    assert ranges[0]["replacement"] == "摘要"


def test_snapshot_verifier_rejects_changed_unprotected_content(tmp_path) -> None:
    path = tmp_path / "a.md"
    path.write_text("changed", encoding="utf-8")
    inputs = RunMaterialInputs.build("r", "msg", [_item("m1", "a.md", "text")])

    class EmptyResult:
        def all(self):
            return []

    class EmptySession:
        async def scalars(self, _statement):
            return EmptyResult()

    with pytest.raises(Exception, match="material_id=m1"):
        asyncio.run(MaterialSnapshotVerifier().verify(EmptySession(), inputs, str(tmp_path)))


def test_snapshot_verifier_uses_protected_version_when_current_changed(tmp_path) -> None:
    path = tmp_path / "a.md"
    path.write_text("changed", encoding="utf-8")
    item = _item("m1", "a.md", "text")
    inputs = RunMaterialInputs.build("r", "msg", [item])

    class VersionResult:
        def all(self):
            return [SimpleNamespace(material_id="m1", digest=item.digest, object_id="b" * 40)]

    class VersionSession:
        async def scalars(self, _statement):
            return VersionResult()

    verified = asyncio.run(MaterialSnapshotVerifier().verify(VersionSession(), inputs, str(tmp_path)))
    assert verified.attached[0].snapshot_object_id == "b" * 40
    assert "git cat-file blob" in MaterialContextProjector.project(verified, str(tmp_path)).policy_text
