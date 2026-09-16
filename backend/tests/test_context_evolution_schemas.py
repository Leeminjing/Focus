r"""本文件验证 Context revision 公共合同的寻址、冻结、序列化与来源顺序语义。

输入为 checkpoint-backed 与 definition-backed revision 样本；输出为相等性、JSON 往返、执行
配置及非法组合断言。具体工作流为通过公共包导入构造引用和完整合同，再验证两种载荷模式不泄漏
散装执行 identity。示例：`pytest backend/tests/test_context_evolution_schemas.py`。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)


def _checkpoint_ref() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="context-root",
        revision_id="revision-root-7",
        generation=7,
        execution_thread_id="thread-root",
        checkpoint_ns="",
        checkpoint_id="checkpoint-root-7",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _definition_ref() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="context-review",
        revision_id="revision-review-2",
        generation=2,
        execution_thread_id="shadow:revision-review-2",
        checkpoint_ns="context-revision",
        checkpoint_id="checkpoint-review-2",
        payload_mode=ContextRevisionPayloadMode.DEFINITION,
    )


@pytest.mark.parametrize("ref", [_checkpoint_ref(), _definition_ref()])
def test_revision_ref_round_trips_with_value_equality(ref: ContextRevisionRef) -> None:
    restored = ContextRevisionRef.model_validate_json(ref.model_dump_json())

    assert restored == ref
    assert restored.model_dump(mode="json") == ref.model_dump(mode="json")
    assert restored.checkpoint_config() == {
        "configurable": {
            "thread_id": ref.execution_thread_id,
            "checkpoint_ns": ref.checkpoint_ns,
            "checkpoint_id": ref.checkpoint_id,
        }
    }


def test_definition_revision_without_prepared_checkpoint_is_not_runnable() -> None:
    ref = _definition_ref().model_copy(update={"checkpoint_id": None})

    assert ref.is_runnable is False
    with pytest.raises(ValueError, match="不可寻址执行历史"):
        ref.checkpoint_config()


def test_checkpoint_revision_requires_checkpoint_and_rejects_extra_identity() -> None:
    payload = _checkpoint_ref().model_dump(mode="json")
    payload["checkpoint_id"] = None

    with pytest.raises(ValidationError, match="必须包含 checkpoint_id"):
        ContextRevisionRef.model_validate(payload)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContextRevisionRef.model_validate({**_checkpoint_ref().model_dump(), "thread": "leak"})


def test_revision_contract_nests_versioned_sources_in_stable_order() -> None:
    source = _checkpoint_ref()
    target = _definition_ref()
    contract = ContextRevisionContract(
        ref=target,
        sources=(ContextRevisionSourceContract(source=source, position=0),),
        authored_messages=({"id": "human-1", "role": "human", "content": "审查架构"},),
        execution_messages=({"id": "human-1", "role": "human", "content": "审查架构"},),
        definition_hash="d" * 64,
        projection_hash="p" * 64,
        content_hash="c" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id="candidate-2",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )

    restored = ContextRevisionContract.model_validate_json(contract.model_dump_json())

    assert restored == contract
    assert restored.sources[0].source == source
    assert restored.model_dump(mode="json")["sources"][0]["source"]["revision_id"] == source.revision_id


def test_revision_contract_rejects_gapped_or_duplicate_sources() -> None:
    common = {
        "ref": _definition_ref(),
        "content_hash": "c" * 64,
        "projection_status": ContextRevisionProjectionStatus.VALID,
        "origin_kind": ContextRevisionOriginKind.CURATION,
        "created_at": datetime(2026, 9, 14, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="连续稳定顺序"):
        ContextRevisionContract(
            **common,
            sources=(ContextRevisionSourceContract(source=_checkpoint_ref(), position=1),),
        )
    with pytest.raises(ValidationError, match="重复引用"):
        ContextRevisionContract(
            **common,
            sources=(
                ContextRevisionSourceContract(source=_checkpoint_ref(), position=0),
                ContextRevisionSourceContract(source=_checkpoint_ref(), position=1),
            ),
        )
