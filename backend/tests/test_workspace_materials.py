"""本文件验证工作区材料的真实接口、owner 隔离、上传幂等与受理清理竞争。

输入为隔离数据库、临时工作区和真实 multipart 文件；输出为无任务上传、同键复用、内容冲突、
已受理保护、私有任务隔离和回滚清理断言。工作流使用现有桌面服务，竞争用例以同源事务锁冻结实际受理窗口。
示例：python -m pytest backend/tests/test_workspace_materials.py。
"""

import asyncio
import hashlib
import io
import os
from pathlib import Path
import uuid

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.datastructures import Headers
import pytest

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.materials.ownership import load_material, lock_workspace_submission
from backend.app.desktop.persistence.models import DesktopMaterial, DesktopThread
from backend.app.desktop.materials.limits import ImageResourceLimits
from backend.app.gateway.app import app


SESSION = {"X-Focus-Session": "focus-dev-session"}


def _png():
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(stream, format="PNG")
    return stream.getvalue()


def _workspace(client, path):
    path.mkdir()
    response = client.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(path)})
    assert response.status_code == 200, response.text
    return response.json()


def _task(client, workspace):
    response = client.post(f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
        headers=SESSION, json={"title": "private material test"})
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client, workspace, submission, item="0", data=None, name="shot.png"):
    return client.post(f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/upload",
        params={"submission_id": submission, "item_id": item}, headers=SESSION,
        files={"file": (name, _png() if data is None else data,
            "image/png" if name.endswith(".png") else "text/plain")})


def _source(data=b"hello"):
    return UploadFile(filename="notes.txt", file=io.BytesIO(data), headers=Headers({"content-type": "text/plain"}))


async def _record_reference(session, workspace, task, material, *, frozen=True):
    loop = AgentLoop(loop_id=uuid.uuid4().hex, workspace_id=workspace["workspace_id"],
        initial_context_id=task["task_id"], holder_id=uuid.uuid4().hex, status="paused")
    session.add(loop)
    await session.flush()
    attachment = {"material_id": material["material_id"], "sha256": material["sha256"]}
    session.add(LoopUserIntent(intent_id=uuid.uuid4().hex, loop_id=loop.loop_id, scope="portfolio",
        content="reference", intent_kind="workspace_input", delivery_state="accepted",
        correlation_id=uuid.uuid4().hex, goal_revision=1, authority_revision=1,
        request_payload={"attachments": [attachment]} if frozen else {"request": {"attachments": [attachment]}}))


def test_workspace_upload_first_send_retry_and_ownership(tmp_path):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        first = _workspace(client, tmp_path / "first")
        other = _workspace(client, tmp_path / "other")
        submission = uuid.uuid4().hex
        response = _upload(client, first, submission)
        assert response.status_code == 200, response.text
        material = response.json()
        assert material["task_id"] is None
        assert material["workspace_id"] == first["workspace_id"]
        assert material["sha256"] == hashlib.sha256(_png()).hexdigest()
        assert material["material_kind"] == "image"
        assert not (Path(first["path"]) / ".git").exists()
        retry = _upload(client, first, submission)
        assert retry.status_code == 200, retry.text
        assert retry.json()["material_id"] == material["material_id"]
        assert len(list((Path(first["path"]) / ".focus/attachments").iterdir())) == 1
        assert _upload(client, other, submission).status_code == 409
        assert _upload(client, first, submission, data=b"different", name="changed.txt").status_code == 409
        root = f"/desktop/api/workspaces/{first['workspace_id']}/materials"
        assert client.get(root, headers=SESSION).json()[0]["material_id"] == material["material_id"]
        preview = client.get(f"{root}/{material['material_id']}/content", headers=SESSION)
        assert preview.status_code == 200 and preview.content == _png()
        foreign = f"/desktop/api/workspaces/{other['workspace_id']}/materials/{material['material_id']}"
        assert client.get(foreign + "/content", headers=SESSION).status_code == 404
        assert client.delete(foreign, headers=SESSION).status_code == 404
        assert client.get("/desktop/api/materials/limits", headers=SESSION).json() == ImageResourceLimits().model_dump()
        assert client.get(f"/desktop/api/workspaces/{first['workspace_id']}", headers=SESSION).json() == first
        assert client.delete(f"{root}/{material['material_id']}", params={"submission_id": "wrong"}, headers=SESSION).status_code == 409
        assert client.delete(f"{root}/{material['material_id']}", params={"submission_id": submission}, headers=SESSION).status_code == 200
        assert not Path(material["path"]).exists()


@pytest.mark.parametrize("git", [False, True])
def test_input_attachment_preview_reads_accepted_version_and_checks_owner(tmp_path, git):
    import subprocess
    from backend.app.desktop.materials.snapshots import MaterialSnapshotVerifier

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        other = _workspace(client, tmp_path / "other")
        if git:
            subprocess.run(["git", "init", workspace["path"]], check=True, capture_output=True)
        task = _task(client, workspace)
        original = _png()
        material = _upload(client, workspace, uuid.uuid4().hex, data=original).json()

        async def seed():
            async with app.state.desktop_service.session_factory.begin() as session:
                row = await session.get(DesktopMaterial, material["material_id"])
                version = await MaterialSnapshotVerifier().protect(session, row, workspace["path"], "workspace_input")
                loop = AgentLoop(loop_id=uuid.uuid4().hex, workspace_id=workspace["workspace_id"],
                    initial_context_id=task["task_id"], holder_id=uuid.uuid4().hex, status="paused")
                session.add(loop)
                await session.flush()
                intent_id = uuid.uuid4().hex
                session.add(LoopUserIntent(intent_id=intent_id, loop_id=loop.loop_id, scope="portfolio",
                    content="preview", intent_kind="workspace_input", delivery_state="accepted",
                    correlation_id=uuid.uuid4().hex, goal_revision=1, authority_revision=1,
                    request_payload={"attachments": [{"material_id": row.material_id, "relative_path": row.relative_path,
                        "digest": row.digest, "sha256": row.digest, "size_bytes": len(original), "material_kind": "image",
                        "snapshot_object_id": version.object_id if version else None}]}))
                return intent_id

        intent_id = client.portal.call(seed)
        path = f"/desktop/api/workspaces/{workspace['workspace_id']}/inputs/{intent_id}/attachments/{material['material_id']}/content"
        response = client.get(path, headers=SESSION)
        assert response.status_code == 200, response.text
        assert response.content == original
        assert response.headers["content-type"] == "image/png"
        assert "filename*=" in response.headers["content-disposition"]
        assert client.get(path.replace(workspace["workspace_id"], other["workspace_id"]), headers=SESSION).status_code == 404
        assert client.get(path.replace(intent_id, uuid.uuid4().hex), headers=SESSION).status_code == 404
        assert client.get(path.replace(material["material_id"], uuid.uuid4().hex), headers=SESSION).status_code == 404
        assert client.get(path).status_code in (401, 403)
        Path(material["path"]).write_bytes(b"changed after sending")
        changed = client.get(path, headers=SESSION)
        assert changed.status_code == (200 if git else 409), changed.text
        if git:
            assert changed.content == original


def test_workspace_upload_validates_bytes_and_cleans_failures(tmp_path, monkeypatch):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        assert _upload(client, workspace, uuid.uuid4().hex, data=b"fake").status_code == 422
        with monkeypatch.context() as limits:
            limits.setattr(app.state.desktop_service.material_uploads, "_limits",
                ImageResourceLimits(general_file_bytes=5, original_image_bytes=4, upload_chunk_bytes=2))
            response = _upload(client, workspace, uuid.uuid4().hex, data=b"123456", name="data.txt")
            assert response.status_code == 413
        directory = Path(workspace["path"]) / ".focus/attachments"
        assert list(directory.iterdir()) == []
        service = app.state.desktop_service.material_uploads
        with monkeypatch.context() as broken:
            broken.setattr(service, "_material", lambda *_: (_ for _ in ()).throw(RuntimeError("registration failed")))
            response = _upload(client, workspace, uuid.uuid4().hex)
            assert response.status_code == 500
        assert list(directory.iterdir()) == []


def test_material_path_escape_is_rejected_without_touching_other_files(tmp_path):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        material = _upload(client, workspace, uuid.uuid4().hex).json()
        outside = tmp_path / "outside.txt"
        outside.write_text("not a material")

        async def corrupt_path():
            async with app.state.desktop_service.session_factory.begin() as session:
                row = await session.get(DesktopMaterial, material["material_id"])
                row.relative_path = "../outside.txt"

        client.portal.call(corrupt_path)
        route = f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/{material['material_id']}"
        assert client.get(route + "/content", headers=SESSION).status_code == 404
        assert client.delete(route, headers=SESSION).status_code == 404
        assert client.delete(f"/desktop/api/materials/{material['material_id']}", headers=SESSION).status_code == 404
        assert outside.read_text() == "not a material"


def test_concurrent_same_upload_reuses_one_file_and_partial_batch(tmp_path):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        submission = uuid.uuid4().hex
        service = app.state.desktop_service.material_uploads

        async def uploads():
            return await asyncio.gather(*[
                service.store_workspace(workspace["workspace_id"], _source(), submission, "0")
                for _ in range(4)
            ])

        results = client.portal.call(uploads)
        assert len({item.material.material_id for item in results}) == 1
        assert _upload(client, workspace, submission, "1", data=b"corrupt").status_code == 422
        assert _upload(client, workspace, submission, "1").status_code == 200
        retry = _upload(client, workspace, submission, "0", data=b"hello", name="notes.txt")
        assert retry.json()["material_id"] == results[0].material.material_id
        assert len(list((Path(workspace["path"]) / ".focus/attachments").iterdir())) == 2


@pytest.mark.parametrize("frozen", [False, True])
def test_accepted_workspace_material_cannot_be_deleted_or_mutated(tmp_path, frozen):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        task = _task(client, workspace)
        material = _upload(client, workspace, uuid.uuid4().hex).json()
        service = app.state.desktop_service

        async def accept():
            async with service.session_factory.begin() as session:
                await _record_reference(session, workspace, task, material, frozen=frozen)

        client.portal.call(accept)
        identity = material["material_id"]
        assert client.delete(f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/{identity}", headers=SESSION).status_code == 409
        assert client.delete(f"/desktop/api/materials/{identity}", headers=SESSION).status_code == 409
        assert client.post(f"/desktop/api/materials/{identity}/clear", headers=SESSION).status_code == 409
        response = client.put(f"/desktop/api/materials/{identity}", headers=SESSION,
            json={"reading_mode": "full", "instruction_mode": "reference", "retention": "removable"})
        assert response.status_code == 409
        Path(material["path"]).write_bytes(b"external change")
        assert client.get(f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/{identity}/content", headers=SESSION).status_code == 409


def test_acceptance_lock_prevents_cleanup_race(tmp_path):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        task = _task(client, workspace)
        submission = uuid.uuid4().hex
        material = _upload(client, workspace, submission).json()
        service = app.state.desktop_service

        async def compete():
            accepted = asyncio.Event()
            release = asyncio.Event()

            async def accept():
                async with service.session_factory.begin() as session:
                    await lock_workspace_submission(session, submission)
                    await load_material(session, material["material_id"], workspace_id=workspace["workspace_id"], lock=True)
                    await _record_reference(session, workspace, task, material)
                    accepted.set()
                    await release.wait()

            accept_task = asyncio.create_task(accept())
            await accepted.wait()
            deletion = asyncio.create_task(service.delete_material(material["material_id"]))
            await asyncio.sleep(0.03)
            assert not deletion.done()
            release.set()
            await accept_task
            with pytest.raises(HTTPException) as error:
                await deletion
            assert error.value.status_code == 409

        client.portal.call(compete)
        assert Path(material["path"]).exists()


def test_workspace_material_survives_task_deletion_and_private_tasks_stay_isolated(tmp_path):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        workspace = _workspace(client, tmp_path / "workspace")
        first, second = _task(client, workspace), _task(client, workspace)
        shared = _upload(client, workspace, uuid.uuid4().hex).json()
        private = client.post(f"/desktop/api/tasks/{first['task_id']}/materials/upload",
            headers=SESSION, files={"file": ("private.txt", b"private", "text/plain")}).json()
        assert client.get(f"/desktop/api/tasks/{second['task_id']}/materials/{private['material_id']}/content", headers=SESSION).status_code == 404
        assert client.get(f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/{private['material_id']}/content", headers=SESSION).status_code == 404
        assert client.get(f"/desktop/api/tasks/{first['task_id']}/materials/{shared['material_id']}/content", headers=SESSION).status_code == 404

        async def remove_task():
            async with app.state.desktop_service.session_factory.begin() as session:
                await session.execute(delete(DesktopThread).where(DesktopThread.task_id == first["task_id"]))

        client.portal.call(remove_task)
        response = client.get(f"/desktop/api/workspaces/{workspace['workspace_id']}/materials/{shared['material_id']}/content", headers=SESSION)
        assert response.status_code == 200
        assert Path(shared["path"]).exists()
