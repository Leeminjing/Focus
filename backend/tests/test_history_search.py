"""History lookup: real catalog filtering, durable filenames and stable reader targets."""

import asyncio
from datetime import datetime, timezone
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.conversations.history import HistorySearchService, history_entries, message_text, snippet
from backend.app.desktop.runs.evidence.models import DesktopDomainResult
from backend.app.desktop.persistence.models import DesktopMaterial, DesktopRun, DesktopThread, DesktopWorkspace, RunMaterialBinding


def test_text_and_retained_compression_sources():
    messages = [{"id": "summary", "role": "ai", "content": "summary", "compression": {
        "source": [{"id": "old", "role": "human", "content": [
            {"type": "text", "text": "登录报错 AUTH"},
            {"type": "image_url", "image_url": {"url": "data:ignored"}},
        ]}]
    }}]
    entries = history_entries(messages)
    assert entries[1] == {"key": "message:0:0", "message_id": "old", "role": "human",
                          "text": "登录报错 AUTH", "retained_source": True}
    assert "report.xlsx" in message_text({"tool_calls": [{"args": {"path": "report.xlsx"}}]})
    assert "AUTH" in snippet("a" * 300 + "AUTH" + "b" * 300, "auth")
    assert len(snippet("a" * 300 + "AUTH" + "b" * 300, "auth")) < 200


def test_pagination_partial_failure_and_literal_matching():
    async def run():
        async def conversation(context_id):
            if context_id == "unavailable":
                raise RuntimeError("checkpoint unavailable")
            return {"messages": [{"id": str(index), "content": "AUTH <error>"} for index in range(7)]}

        service = HistorySearchService(None, SimpleNamespace(live_conversation=conversation))
        tasks = [{"context_id": key, "title": key, "workspace_name": "work", "lifecycle": "active"}
                 for key in ["living", "unavailable"]]
        service._catalog = AsyncMock(return_value=(tasks, {"living": [], "unavailable": []}))
        first = await service.search("auth", limit=3)
        second = await service.search("auth", offset=3, limit=3)
        third = await service.search("auth", offset=6, limit=3)
        assert first["has_more"] and second["has_more"] and not third["has_more"]
        assert len({hit["key"] for page in [first, second, third] for hit in page["results"]}) == 7
        assert first["unavailable_context_ids"] == ["unavailable"]
        assert not (await service.search(".*"))["results"]
        assert not (await service.search("   "))["results"]
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_catalog_archives_deleted_contexts_and_material_history():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id, active_id, archived_id, deleted_id, run_id = [uuid.uuid4().hex for _ in range(5)]
        calls = []

        async def conversation(context_id):
            calls.append(context_id)
            return {"messages": [{"id": "input", "role": "human", "content": "登录报错"},
                                 {"id": "output", "role": "ai", "content": "已生成 outputs/report.xlsx"}]}

        service = HistorySearchService(factory, SimpleNamespace(live_conversation=conversation))
        now = datetime.now(timezone.utc)
        try:
            async with factory() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="history workspace"))
                await session.flush()
                for task_id, title, archived, deleted in [
                    (active_id, "active title", None, None),
                    (archived_id, "archived title", now, None),
                    (deleted_id, "deleted title", now, now),
                ]:
                    session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=task_id,
                                              title=title, archived_at=archived, deleted_at=deleted))
                await session.flush()
                session.add(DesktopRun(run_id=run_id, task_id=archived_id, agent_id=f"main:{archived_id}", kind="main", status="success"))
                session.add(DesktopMaterial(material_id=uuid.uuid4().hex, task_id=archived_id, relative_path="outputs/export.csv"))
                await session.flush()
                session.add(RunMaterialBinding(
                    binding_id=uuid.uuid4().hex, run_id=run_id, task_id=archived_id, message_id="input",
                    material_id_snapshot=uuid.uuid4().hex, relative_path_snapshot="removed/login.log",
                    digest_snapshot="a" * 64, material_kind_snapshot="text", note="原始日志", ordinal=0,
                ))
                session.add(DesktopDomainResult(
                    result_key=uuid.uuid4().hex, kind="artifact", source_id=uuid.uuid4().hex,
                    version="v1", context_id=archived_id, run_id=run_id,
                    payload={"path": "outputs/durable-deliverable.pdf"},
                ))
                await session.commit()

            response = await service.search("登录报错")
            assert {hit["context_id"] for hit in response["results"]} == {active_id, archived_id}
            assert deleted_id not in calls
            hit = (await service.search("login.log"))["results"][0]
            assert hit["kind"] == "file" and hit["context_id"] == archived_id
            reader = await service.read(archived_id)
            assert reader["read_only"] is True and reader["lifecycle"] == "archived"
            assert any(entry["key"] == hit["key"] for entry in reader["entries"])
            assert (await service.search("export.csv"))["results"][0]["kind"] == "file"
            assert (await service.search("report.xlsx"))["results"][0]["message_id"] == "output"
            artifact = (await service.search("durable-deliverable.pdf"))["results"][0]
            assert artifact["context_id"] == archived_id and artifact["kind"] == "file"
            assert any(item["key"] == artifact["key"] and item["kind"] == "artifact" for item in reader["materials"])
            assert (await service.search("archived title"))["results"][0]["kind"] == "task"
            assert (await service.search("history workspace"))["results"]
            with pytest.raises(HTTPException) as error:
                await service.read(deleted_id)
            assert error.value.status_code == 404
            async with factory() as session:
                assert (await session.get(DesktopThread, archived_id)).archived_at == now
        finally:
            async with factory() as session:
                await session.execute(delete(DesktopDomainResult).where(DesktopDomainResult.context_id == archived_id))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
                await session.commit()
            await engine.dispose()
    asyncio.run(run())
