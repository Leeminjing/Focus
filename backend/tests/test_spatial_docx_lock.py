"""DOCX 外部占用时的原文件保护、领域终态与错误投影回归测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from langchain.tools import ToolRuntime

from plugins.spatial_patrol.docx_edit import (
    delete_docx_paragraph,
    observe_docx_delete_candidate,
)
from plugins.spatial_patrol.routes import _anchor_payload, _spatial_terminal_status
from backend.tests.spatial_context_support import spatial_tool_runtime


def _runtime(workspace: Path, y: float, evidence: dict, candidates: dict) -> ToolRuntime:
    return spatial_tool_runtime(
        workspace=workspace, y=y, run_id="run-locked",
        change_evidence=evidence, docx_candidates=candidates,
    )


def _save_document(path: Path, paragraphs: list[str]) -> None:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))


def _paragraph_y(paragraphs: list[str], index: int) -> float:
    extracted = "\n".join(paragraphs)
    start = sum(len(text) + 1 for text in paragraphs[:index])
    return (start + len(paragraphs[index]) / 2) / len(extracted)


def test_locked_target_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    paragraphs = ["标题", "（含页眉层水印“Trae ai”）", "摘要"]
    target = tmp_path / "sample.docx"
    _save_document(target, paragraphs)
    before = target.read_bytes()
    evidence: dict = {}
    candidates: dict = {}
    runtime = _runtime(tmp_path, _paragraph_y(paragraphs, 1), evidence, candidates)
    observation = observe_docx_delete_candidate.func(runtime=runtime)

    def reject_replace(_source, _target):
        raise PermissionError("[WinError 5] 拒绝访问")

    monkeypatch.setattr("plugins.spatial_patrol.docx_edit.os.replace", reject_replace)

    with pytest.raises(PermissionError) as caught:
        delete_docx_paragraph.func(
            candidate_id=observation["candidate_id"], runtime=runtime,
        )

    assert str(caught.value) == "DOCX 正被其他程序占用，请关闭 WPS/Word 中打开的该文件后重试"
    assert target.read_bytes() == before
    assert list(tmp_path.glob(".sample.*.docx")) == []
    assert evidence == {}


def test_error_run_becomes_needs_action_and_projects_existing_error():
    assert _spatial_terminal_status(
        "error", requires_verified_change=True, change_evidence={"changed": True}
    ) == "needs_action"
    for status in ("pending", "running", "interrupted"):
        assert _spatial_terminal_status(
            status, requires_verified_change=True, change_evidence=None
        ) == "deployed"

    anchor = SimpleNamespace(to_payload=lambda: {"spatial_id": "soldier-1"})
    run = SimpleNamespace(status="error", error="DOCX 正被其他程序占用")
    assert _anchor_payload(anchor, run) == {
        "spatial_id": "soldier-1",
        "run_status": "error",
        "run_error": "DOCX 正被其他程序占用",
    }
