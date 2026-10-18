"""File-link HTTP reads exercise real path selection, worktrees and Git snapshots."""
import hashlib
import json
import subprocess
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.desktop.file_links import file_link_router
from backend.app.desktop.models import DesktopThread, DesktopWorkspace, DesktopRun, DesktopMaterial, MaterialVersion
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class Session:
    def __init__(self, rows):
        self.rows = {(type(row), getattr(row, key)): row for row in rows for key in (
            "task_id" if isinstance(row, DesktopThread) else "workspace_id" if isinstance(row, DesktopWorkspace)
            else "run_id" if isinstance(row, DesktopRun) else "slot_id" if isinstance(row, WorkspaceSlot)
            else "material_id" if isinstance(row, DesktopMaterial) else "version_id",
        )}
        self.material = next((row for row in rows if isinstance(row, DesktopMaterial)), None)

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def get(self, model, key): return self.rows.get((model, key))
    async def scalar(self, statement): return self.material
    async def scalars(self, statement):
        sql = str(statement)
        rows = [row for row in self.rows.values() if isinstance(row, WorkspaceSlot)] if "FROM workspace_slots" in sql else [
            row.workspace_anchor for row in self.rows.values() if isinstance(row, DesktopRun)]
        return SimpleNamespace(all=lambda: rows)


@pytest.fixture
def files(tmp_path):
    root, tree = tmp_path / "workspace", tmp_path / "worktree"
    root.mkdir(); tree.mkdir()
    for directory, value in ((root, "root"), (root / "docs", "docs"), (root / "其他目录", "other"), (tree, "worktree")):
        directory.mkdir(exist_ok=True)
        (directory / "report.md").write_text(value, encoding="utf-8")
    (root / "docs" / "中文 报告.md").write_text("中文内容", encoding="utf-8")
    (root / "docs" / "%20.md").write_text("literal percent", encoding="utf-8")
    workspace = DesktopWorkspace(workspace_id="w", path=str(root))
    task = DesktopThread(task_id="t", workspace_id="w", thread_id="thread", title="test")
    slot = WorkspaceSlot(slot_id="s", workspace_id="w", root_path=str(tree), lifecycle="retained", kind="isolated")
    run = DesktopRun(run_id="r", task_id="t", workspace_anchor={"slot_id": "s"})
    rows = [workspace, task, slot]
    app = FastAPI()
    app.include_router(file_link_router, prefix="/desktop/api")
    from backend.app.desktop.file_routes import file_router
    app.include_router(file_router, prefix="/desktop/api")
    session = Session(rows)
    app.state.desktop_service = SimpleNamespace(session_factory=lambda: session)
    with TestClient(app) as client:
        yield client, root, tree, session, run


def read(client, **reference):
    return client.post("/desktop/api/tasks/t/file-links/content", json={"workspace_id": "w", **reference})


def test_file_search_keeps_full_paths_and_workspace_scope(files):
    client, root, tree, _, _ = files
    (root / "src").mkdir()
    (root / "src" / "dispatch.py").write_text("submit()", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dispatch.py").write_text("ignored", encoding="utf-8")
    (tree / "dispatch.py").write_text("another workspace", encoding="utf-8")
    response = client.get("/desktop/api/tasks/t/files/search", params={"q": "DISPATCH"})
    assert response.status_code == 200
    assert response.json()["files"] == [{"path": str((root / "src" / "dispatch.py").resolve()),
        "relative_path": "src/dispatch.py", "workspace_id": "w"}]
    assert client.get("/desktop/api/tasks/t/files/search", params={"q": "../worktree"}).json()["files"] == []
    assert client.get("/desktop/api/tasks/missing/files/search").status_code == 404
    assert client.get("/desktop/api/tasks/t/files/search", params={"limit": 0}).status_code == 422
    assert len(client.get("/desktop/api/tasks/t/files/search", params={"limit": 1}).json()["files"]) == 1
    assert client.get("/desktop/api/tasks/t/files/search", params={"q": "中文"}).json()["files"][0]["relative_path"] == "docs/中文 报告.md"


def test_line_ranges_survive_validation_and_preview_requests(files):
    client, _, _, _, _ = files
    response = read(client, path="report.md", start_line=2, end_line=4)
    assert response.status_code == 200
    identity = json.loads(unquote(response.headers["X-Focus-File-Reference"]))
    assert (identity["start_line"], identity["end_line"]) == (2, 4)
    identity = json.loads(unquote(read(client, path="report.md", start_line=2).headers["X-Focus-File-Reference"]))
    assert identity["end_line"] == 2
    for fields in ({"start_line": 0}, {"start_line": 5, "end_line": 2}, {"end_line": 2}):
        assert read(client, path="report.md", **fields).status_code == 422
    target = {"task_id": "t", "link": {"path": "report.md", "start_line": 1, "end_line": 1}}
    assert client.post("/desktop/api/files/content?preview=true", json=target).text == "root"


def test_exact_paths_and_native_windows_drive(files):
    client, root, tree, _, _ = files
    for path, expected in [("docs/report.md", "docs"), ("其他目录/report.md", "other"),
            ("docs/中文 报告.md", "中文内容"), ("docs/%20.md", "literal percent"),
            (str(root / "docs" / "report.md"), "docs"), (str(tree / "report.md"), "worktree")]:
        response = read(client, path=path)
        assert response.status_code == 200, response.text
        assert response.text == expected
        identity = json.loads(unquote(response.headers["X-Focus-File-Reference"]))
        assert identity["workspace_id"] == "w" and identity["view"] == "current"
        assert identity["path"].endswith(path.replace("\\", "/").split("/")[-1])
    assert read(client, path="missing/report.md").status_code == 404
    assert read(client, path="docs/report.md", workspace_id="foreign").status_code == 404
    assert read(client, path="../worktree/report.md").status_code == 404


def test_worktree_source_never_falls_back(files):
    client, root, tree, session, run = files
    session.rows[(DesktopRun, "r")] = run
    assert read(client, path="report.md", run_id="r").text == "worktree"
    assert read(client, path="report.md", slot_id="s").text == "worktree"
    assert read(client, path="report.md").status_code == 409
    assert read(client, path="report.md", run_id="r", slot_id="foreign").status_code == 409
    (tree / "report.md").unlink()
    assert read(client, path="report.md", run_id="r").status_code == 404
    assert (root / "report.md").read_text() == "root"
    session.rows[(WorkspaceSlot, "s")].lifecycle = "deleted"
    assert read(client, path="report.md", slot_id="s").status_code == 404


def test_shared_previews_and_native_exits_keep_link_identity(files):
    client, root, tree, session, run = files
    session.rows[(DesktopRun, "r")] = run
    (tree / "data.csv").write_text("name,value\n正确工作树,1", encoding="utf-8")
    (root / "data.csv").write_text("wrong root", encoding="utf-8")
    target = {"task_id": "t", "link": {"path": "data.csv", "workspace_id": "w", "run_id": "r", "slot_id": "s"}}
    response = client.post("/desktop/api/files/content?preview=true", json=target)
    assert response.status_code == 200 and "正确工作树" in response.text
    location = client.post("/desktop/api/files/location", json={**target, "current": True})
    assert location.status_code == 200 and location.json()["path"] == str((tree / "data.csv").resolve())
    target["link"]["view"] = "history"
    assert client.post("/desktop/api/files/location", json={**target, "current": True}).status_code == 422


def test_git_history_stays_frozen_after_update_and_delete(files):
    client, root, _, session, _ = files
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    path = root / "docs" / "report.md"
    old = path.read_bytes()
    object_id = subprocess.check_output(["git", "-C", str(root), "hash-object", "-w", str(path)], text=True).strip()
    material = DesktopMaterial(material_id="m", task_id="t", relative_path="docs/report.md")
    version = MaterialVersion(version_id="v", material_id="m", object_id=object_id, digest=hashlib.sha256(old).hexdigest())
    session.rows[(MaterialVersion, "v")] = version
    session.material = material
    path.write_text("latest", encoding="utf-8")
    ref = dict(path="docs/report.md", version_id="v", view="history")
    assert read(client, **ref).text == "docs"
    assert read(client, path="docs/report.md").text == "latest"
    path.unlink()
    assert read(client, **ref).text == "docs"
    assert read(client, path="docs/report.md").status_code == 404
    assert read(client, **{**ref, "path": "report.md"}).status_code == 409
    version.object_id = "0" * 40
    response = read(client, **ref)
    assert response.status_code == 409 and "版本不可用" in response.text


def test_missing_history_never_reads_latest(files):
    client, root, _, _, _ = files
    response = read(client, path="report.md", view="history")
    assert response.status_code == 409
    assert "未保存对应内容" in response.json()["detail"] and "root" not in response.text
    assert read(client, path="report.md", version_id="missing").status_code == 422
    assert read(client, path="report.md", view="history", version_id="missing").status_code == 404


def test_fact_history_uses_persisted_revision(files, monkeypatch):
    from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRevision
    from backend.app.desktop.agent_loop.models import AgentLoop
    from backend.app.desktop.domain_evidence.models import DesktopDomainResult
    from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy

    client, _, _, session, run = files
    fact = LoopFact(fact_id="fact", loop_id="loop", fact_type="artifact", current_revision=2)
    source = DesktopDomainResult(result_key="result", run_id="r", loop_id="loop", kind="artifact", payload={
        "format": "file_version_v1", "path": "report.md", "slot_id": "s", "sha256": "old-hash", "run_id": "r",
        "after": {"available": True, "text": "frozen version one"},
    })
    session.rows.update({(LoopFact, "fact"): fact, (AgentLoop, "loop"): AgentLoop(loop_id="loop", workspace_id="w"),
        (DesktopDomainResult, "result"): source, (DesktopRun, "r"): run})
    revision = LoopFactRevision(fact_id="fact", revision=1, evidence=[{"kind": "artifact", "metadata": {
        "file": {"result_key": "result"}}}])
    async def scalar(statement):
        assert "loop_fact_revisions.revision" in str(statement)
        assert statement.compile().params["revision_1"] == 1
        return revision
    session.scalar = scalar
    async def permissions(*args): return SimpleNamespace(permissions=frozenset({"read"}))
    monkeypatch.setattr(LoopLiveAccessPolicy, "resolve", permissions)
    ref = dict(path="report.md", view="history", fact_id="fact", revision=1, slot_id="s", result_key="result", sha256="old-hash")
    response = read(client, **ref)
    assert response.status_code == 200, response.text
    assert response.text == "frozen version one"
    assert read(client, **{**ref, "sha256": "new-hash"}).status_code == 409
    source.payload["after"] = {"available": False, "reason": "over_1_mib"}
    response = read(client, **ref)
    assert response.status_code == 409 and "未保存可展示" in response.text
