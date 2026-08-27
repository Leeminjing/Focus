import zipfile

from plugins.spatial_patrol.docx_sessions import SessionError, document_id, resolve_workspace_docx


def _write_docx(path):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("_rels/.rels", "<Relationships/>")
        package.writestr("word/document.xml", "<document/>")


def test_workspace_path_is_normalized_and_cannot_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "test.docx"
    _write_docx(target)
    assert resolve_workspace_docx(workspace, "test.docx") == target.resolve()
    for value in ("../test.docx", str(target.resolve()), "test.txt"):
        try:
            resolve_workspace_docx(workspace, value)
            raise AssertionError("unsafe path should fail")
        except SessionError:
            pass


def test_document_id_is_stable_across_separator_spelling():
    assert document_id("task", "folder/file.docx") == document_id("task", "folder\\file.docx")
