"""本文件验证通用逐轮材料、图片派生、历史与自定义分组 HTTP 契约。

输入为两个任务、真实上传、结构化 material_inputs、独立必看图片和分组操作；输出为任务隔离、
备注历史、删除后快照、单事务失败回滚、图片预算、稳定 origin 身份和分组优先级断言。具体
工作流只替换网关后台启动函数，桌面服务、数据库、迁移和文件链路保持真实。

示例：python -m pytest backend/tests/test_image_material_api.py。
"""

import asyncio
import io
import os
from pathlib import Path
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.desktop.resource_limits import ImageResourceLimits  # noqa: E402
from backend.app.gateway.app import app  # noqa: E402
from focus.runtime.runs.manager import RunRecord  # noqa: E402
from focus.runtime.runs.schemas import DisconnectMode, RunStatus  # noqa: E402


SESSION = {"X-Focus-Session": "focus-dev-session"}


def _png(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _workspace_and_task(client: TestClient, root: Path, title: str) -> tuple[dict, dict]:
    root.mkdir()
    workspace = client.post(
        "/desktop/api/workspaces",
        headers=SESSION,
        json={"path": str(root)},
    ).json()
    task = client.post(
        f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
        headers=SESSION,
        json={"thread_id": f"image-api-{uuid.uuid4().hex}", "title": title},
    ).json()
    return workspace, task


def _upload(client: TestClient, task_id: str, name: str, data: bytes, mime: str):
    return client.post(
        f"/desktop/api/tasks/{task_id}/materials/upload",
        headers=SESSION,
        files={"file": (name, data, mime)},
    )


def test_image_material_api_contract_and_run_boundaries(tmp_path, monkeypatch) -> None:
    import backend.app.desktop.routes as desktop_routes

    launched = []

    async def fake_start_run(body, thread_id, request, agent_factory=None):
        launched.append((body, thread_id, agent_factory))
        done = asyncio.Future()
        done.set_result(None)
        return RunRecord(
            run_id=body.context["run_id"],
            thread_id=thread_id,
            status=RunStatus.success,
            on_disconnect=DisconnectMode.continue_,
            task=done,
        )

    original_start_run = desktop_routes.start_run
    desktop_routes.start_run = fake_start_run
    try:
        with _client() as client:
            _, first = _workspace_and_task(client, tmp_path / "first", "first")
            _, second = _workspace_and_task(client, tmp_path / "second", "second")
            first_id, second_id = first["task_id"], second["task_id"]

            image_bytes = _png()
            image = _upload(client, first_id, "shot.png", image_bytes, "image/png")
            assert image.status_code == 200
            image_id = image.json()["material_id"]
            text = _upload(client, first_id, "notes.txt", b"hello", "text/plain")
            assert text.status_code == 200
            text_id = text.json()["material_id"]

            group = client.post(
                f"/desktop/api/tasks/{first_id}/material-groups",
                headers=SESSION,
                json={"name": "需求资料"},
            )
            assert group.status_code == 200
            group_id = group.json()["group_id"]
            moved = client.put(
                f"/desktop/api/tasks/{first_id}/materials/{text_id}/group",
                headers=SESSION,
                json={"group_id": group_id, "position": 0},
            )
            assert moved.status_code == 200
            listed_materials = client.get(
                f"/desktop/api/tasks/{first_id}/materials", headers=SESSION
            ).json()
            assert next(item for item in listed_materials if item["material_id"] == text_id)["custom_group_id"] == group_id
            assert client.put(
                f"/desktop/api/tasks/{second_id}/materials/{text_id}/group",
                headers=SESSION,
                json={"group_id": group_id, "position": 0},
            ).status_code == 404
            restored_auto = client.put(
                f"/desktop/api/tasks/{first_id}/materials/{text_id}/group",
                headers=SESSION,
                json={"group_id": None, "position": 0},
            )
            assert restored_auto.status_code == 200
            assert restored_auto.json()["membership"] is None
            assert client.put(
                f"/desktop/api/tasks/{first_id}/materials/{text_id}/group",
                headers=SESSION,
                json={"group_id": group_id, "position": 0},
            ).status_code == 200
            assert client.delete(
                f"/desktop/api/tasks/{first_id}/material-groups/{group_id}", headers=SESSION
            ).status_code == 409
            assert client.delete(
                f"/desktop/api/tasks/{first_id}/material-groups/{group_id}?confirm=true", headers=SESSION
            ).status_code == 200
            assert any(
                item["material_id"] == text_id
                for item in client.get(f"/desktop/api/tasks/{first_id}/materials", headers=SESSION).json()
            )
            concurrent_group = client.post(
                f"/desktop/api/tasks/{first_id}/material-groups",
                headers=SESSION,
                json={"name": "并发归档"},
            ).json()
            service = app.state.desktop_service

            async def move_concurrently() -> None:
                await asyncio.gather(
                    service.material_groups.move(first_id, text_id, concurrent_group["group_id"], 0),
                    service.material_groups.move(first_id, image_id, concurrent_group["group_id"], 0),
                )

            client.portal.call(move_concurrently)
            concurrent_members = client.get(
                f"/desktop/api/tasks/{first_id}/material-groups", headers=SESSION
            ).json()[0]["memberships"]
            assert {item["material_id"] for item in concurrent_members} == {text_id, image_id}
            assert sorted(item["position"] for item in concurrent_members) == [0, 1]

            with monkeypatch.context() as path_guard:
                path_guard.setattr(
                    Path,
                    "read_bytes",
                    lambda _path: (_ for _ in ()).throw(AssertionError("内容 API 不得整块读取")),
                )
                content = client.get(
                    f"/desktop/api/tasks/{first_id}/materials/{image_id}/content",
                    headers=SESSION,
                )
            assert content.status_code == 200
            assert content.headers["content-type"].startswith("image/png")
            assert content.content == image_bytes
            assert client.get(
                f"/desktop/api/tasks/{second_id}/materials/{image_id}/content",
                headers=SESSION,
            ).status_code == 404

            detached = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "must_view_material_ids": [image_id]},
            )
            assert detached.status_code == 422
            foreign = client.post(
                f"/desktop/api/tasks/{second_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "attached_material_ids": [image_id]},
            )
            assert foreign.status_code == 422
            non_image = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "material_inputs": [{"material_id": text_id, "note": "只看标题"}]},
            )
            assert non_image.status_code == 200
            second_text_use = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x2", "material_inputs": [{"material_id": text_id, "note": "第二轮备注"}]},
            )
            assert second_text_use.status_code == 200
            text_history = client.get(
                f"/desktop/api/tasks/{first_id}/material-history?material_id={text_id}", headers=SESSION
            ).json()
            assert [item["note"] for item in text_history[-2:]] == ["只看标题", "第二轮备注"]
            mixed = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "material_inputs": [], "attached_material_ids": []},
            )
            assert mixed.status_code == 422
            missing = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "attached_material_ids": ["missing"]},
            )
            assert missing.status_code == 422
            too_many = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={
                    "message": "x",
                    "material_inputs": [
                        {"material_id": f"m-{index}", "note": ""} for index in range(65)
                    ],
                },
            )
            assert too_many.status_code == 413

            history_before_failure = len(client.get(
                f"/desktop/api/tasks/{first_id}/material-history?material_id={text_id}",
                headers=SESSION,
            ).json())
            original_history_add = service.run_material_history.add
            service.run_material_history.add = lambda *_args: (_ for _ in ()).throw(RuntimeError("binding failure"))
            with pytest.raises(RuntimeError, match="binding failure"):
                client.post(
                    f"/desktop/api/tasks/{first_id}/main/runs",
                    headers=SESSION,
                    json={"message": "transaction", "material_inputs": [{"material_id": text_id}]},
                )
            service.run_material_history.add = original_history_add
            assert len(client.get(
                f"/desktop/api/tasks/{first_id}/material-history?material_id={text_id}",
                headers=SESSION,
            ).json()) == history_before_failure

            original_limits = service.run_materials._images._limits
            service.run_materials._images._limits = ImageResourceLimits(run_model_bytes=1)
            aggregate = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "attached_material_ids": [image_id]},
            )
            assert aggregate.status_code == 413
            service.run_materials._images._limits = original_limits

            original_window = service._validate_model_window
            measured = []
            service._validate_model_window = lambda _model, used: measured.append(used)
            plain = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x"},
            )
            assert plain.status_code == 200
            plain_tokens = measured[-1]

            def enforce_plain_window(_model, used):
                if used > plain_tokens:
                    raise HTTPException(422, "窗口超限")

            service._validate_model_window = enforce_plain_window
            window = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "attached_material_ids": [image_id]},
            )
            assert window.status_code == 422
            note_window = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={"message": "x", "material_inputs": [{"material_id": text_id, "note": "需要额外计量"}]},
            )
            assert note_window.status_code == 422
            service._validate_model_window = original_window

            accepted = client.post(
                f"/desktop/api/tasks/{first_id}/main/runs",
                headers=SESSION,
                json={
                    "message": "x",
                    "material_inputs": [
                        {"material_id": text_id, "note": "结合文档"},
                        {"material_id": image_id, "note": "逐像素核对\n保留原文"},
                    ],
                    "must_view_material_ids": [image_id],
                },
            )
            assert accepted.status_code == 200
            context = launched[-1][0].context["run_image_inputs"]
            assert [item["material_id"] for item in context["attached"]] == [image_id]
            assert context["required_ids"] == [image_id]
            run_materials = launched[-1][0].context["run_material_inputs"]
            assert [item["material_id"] for item in run_materials["attached"]] == [text_id, image_id]
            assert [item["note"] for item in run_materials["attached"]] == ["结合文档", "逐像素核对\n保留原文"]
            assert run_materials["origin_run_id"] == accepted.json()["run_id"]
            human = launched[-1][0].input["messages"][0]
            assert human["role"] == "human"
            assert human["id"] == accepted.json()["message_id"] == run_materials["origin_message_id"]
            assert "逐像素核对" in human["content"]
            history = client.get(
                f"/desktop/api/tasks/{first_id}/material-history?material_id={image_id}",
                headers=SESSION,
            )
            assert history.status_code == 200
            assert history.json()[-1]["note"] == "逐像素核对\n保留原文"
            assert history.json()[-1]["message_id"] == accepted.json()["message_id"]
            assert client.delete(
                f"/desktop/api/materials/{image_id}", headers=SESSION
            ).status_code == 200
            deleted_history = client.get(
                f"/desktop/api/tasks/{first_id}/material-history?material_id={image_id}",
                headers=SESSION,
            ).json()
            assert deleted_history[-1]["current_available"] is False
            assert client.get(
                f"/desktop/api/tasks/{second_id}/material-history?material_id={image_id}",
                headers=SESSION,
            ).status_code == 404

            empty = _upload(client, first_id, "empty.png", b"", "image/png")
            assert empty.status_code == 422
            stale = _upload(client, first_id, "stale.png", image_bytes, "image/png").json()
            stale_path = Path(tmp_path / "first", *Path(stale["relative_path"]).parts)
            stale_path.unlink()
            assert client.get(
                f"/desktop/api/tasks/{first_id}/materials/{stale['material_id']}/image-validity",
                headers=SESSION,
            ).status_code == 422
            assert client.get(
                f"/desktop/api/tasks/{first_id}/materials/{stale['material_id']}/content",
                headers=SESSION,
            ).status_code == 404
    finally:
        desktop_routes.start_run = original_start_run
