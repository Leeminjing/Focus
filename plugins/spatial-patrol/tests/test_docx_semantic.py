from plugins.spatial_patrol.docx_semantic import (
    DocxSemanticRegion,
    SemanticTarget,
    is_legacy_docx_region,
    recover_target,
)


def _target(target_id, *, stable_id=None, index=0, exact="unique text"):
    return SemanticTarget.model_validate({
        "target_id": target_id,
        "kind": "paragraph",
        "stable_id": stable_id,
        "structure_path": [{"kind": "paragraph", "index": index}],
        "fingerprint": {"exact": exact, "prefix": exact[:4], "suffix": exact[-4:]},
    })


def _region(target):
    return DocxSemanticRegion.model_validate({
        "document_id": "d" * 64,
        "document_version": 3,
        "placement": {"page": 2, "x": .25, "y": .25},
        "target": target.model_dump(),
        "projection": {"page": 2, "rect": {"x": .1, "y": .2, "width": .3, "height": .1}},
    })


def test_recovery_prefers_stable_id_then_structure_then_unique_fingerprint():
    expected = _target("old", stable_id="stable", index=2)
    moved = _target("new", stable_id="stable", index=8)
    assert recover_target(_region(expected), [moved]).target_id == "new"

    expected = _target("old", index=2)
    structural = _target("new", index=2)
    assert recover_target(_region(expected), [structural]).target_id == "new"

    moved = _target("new", index=9)
    assert recover_target(_region(expected), [moved]).target_id == "new"


def test_recovery_never_attaches_when_fingerprint_is_ambiguous():
    expected = _target("old", index=2)
    candidates = [_target("a", index=7), _target("b", index=8)]
    assert recover_target(_region(expected), candidates) is None
    assert is_legacy_docx_region({"coordinate_space": "text-character-v1"})
    assert not is_legacy_docx_region(_region(expected).model_dump())


def test_semantic_region_requires_exact_page_placement():
    payload = _region(_target("paragraph:one")).model_dump()
    payload.pop("placement")

    try:
        DocxSemanticRegion.model_validate(payload)
    except ValueError as exc:
        assert "placement" in str(exc)
    else:
        raise AssertionError("DOCX semantic anchors must require exact placement")


def test_page_region_keeps_the_clicked_point_as_spatial_truth():
    region = DocxSemanticRegion.model_validate({
        "document_id": "d" * 64,
        "document_version": 3,
        "placement": {"page": 3, "x": .71, "y": .42},
        "target": {
            "target_id": "page:3:710000:420000",
            "kind": "page_region",
            "structure_path": [{"kind": "page", "index": 2}],
            "fingerprint": {},
        },
        "projection": {
            "page": 3,
            "rect": {"x": .71, "y": .42, "width": 0, "height": 0},
        },
    })

    assert region.placement.model_dump() == {"page": 3, "x": .71, "y": .42}
    assert region.target.kind == "page_region"
