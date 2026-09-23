r"""本文件对外提供 frozen evidence corpus authority 的纯验证测试。

输入为冻结 observation、精确 manifest/source 与模拟存储 content hash；输出为成功验证或 stale/out-of-scope 稳定错误。
具体工作流为直接调用 FrozenEvidenceAuthority，不连接数据库、不读取 latest Context。示例：
`pytest backend/tests/test_semantic_evidence_corpus.py -q`。
"""

from __future__ import annotations

import pytest

from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import (
    EvidenceCorpusReadError,
    FrozenEvidenceAuthority,
    FrozenEvidenceCorpusReader,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_manifest import (
    SemanticManifestProjector,
)
from backend.tests.test_semantic_context_planning import _observation


def test_exact_frozen_source_is_authorized_without_reading_latest() -> None:
    observation = _observation()
    manifest = SemanticManifestProjector().project(observation)[0]

    FrozenEvidenceAuthority().validate_source(
        observation,
        manifest.source,
        manifest,
        "c" * 64,
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("newer_revision", "stale_source"),
        ("hash_mismatch", "stale_source"),
        ("out_of_scope", "source_out_of_scope"),
    ),
)
def test_frozen_source_drift_has_distinct_authority_error(mutation: str, expected: str) -> None:
    observation = _observation()
    manifest = SemanticManifestProjector().project(observation)[0]
    source = manifest.source
    stored_hash = "c" * 64
    if mutation == "newer_revision":
        source = source.model_copy(
            update={"revision_id": "revision-planning-2", "generation": 2, "checkpoint_id": "checkpoint-planning-2"}
        )
    elif mutation == "hash_mismatch":
        stored_hash = "d" * 64
    else:
        observation = observation.model_copy(update={"grant": {"context_scope": ("different-context",)}})

    with pytest.raises(EvidenceCorpusReadError) as raised:
        FrozenEvidenceAuthority().validate_source(observation, source, manifest, stored_hash)

    assert raised.value.code == expected


def test_out_of_scope_material_is_rejected_before_corpus_construction() -> None:
    observation = _observation().model_copy(
        update={
            "grant": {
                "context_scope": ("context-planning",),
                "material_scope": ("allowed-material",),
            },
            "workspace": {
                "revision": 7,
                "materials": (
                    {
                        "material_id": "outside-material",
                        "version_id": "version-1",
                        "content_hash": "e" * 64,
                        "content": "Frozen material.",
                    },
                ),
            },
        }
    )

    with pytest.raises(EvidenceCorpusReadError) as raised:
        FrozenEvidenceCorpusReader._structured(observation)

    assert raised.value.code == "source_out_of_scope"
