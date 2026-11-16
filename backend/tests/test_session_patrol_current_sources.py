"""Current Context imports remain independent after the source advances or disappears."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.desktop.patrol.session_patrol.availability import (
    SourceAvailabilityReader,
)
from backend.app.desktop.patrol.session_patrol.source_projection import (
    freeze_authoring_sources,
)
from backend.app.desktop.patrol.session_patrol.source_reader import PatrolSourceReader


def test_context_source_reads_current_and_rejects_changed_preview_or_history():
    state = SimpleNamespace(
        ref=SimpleNamespace(execution_thread_id="thread", checkpoint_ns="", checkpoint_id="checkpoint"),
        content_hash="current-hash", history_payload=None,
    )
    messages = [{"role": "user", "content": "current body"}]
    task = SimpleNamespace(task_id="context", deleted_at=None, title="会话")

    class Repository:
        async def current(self, _session, context_id):
            assert context_id == "context"
            return state

    class Reader:
        async def read(self, _session, ref, view):
            assert ref is state.ref and view == "execution"
            return SimpleNamespace(messages=messages)

    class Session:
        async def get(self, _model, context_id):
            return task if context_id == task.task_id else None

    async def check():
        reader = PatrolSourceReader(SimpleNamespace(checkpointer=None))
        reader._repository, reader._reader = Repository(), Reader()
        records, source = await reader.read(Session(), {"kind": "context", "context_id": "context"})
        assert records == messages and source["context_hash"] == "current-hash"
        assert "revision_id" not in source
        for request, code, status in [
            ({"context_id": "context", "context_hash": "old-hash"}, "source_changed", 409),
            ({"context_id": "context", "revision_id": "old"}, "context_history_removed", 422),
        ]:
            with pytest.raises(HTTPException) as error:
                await reader.read(Session(), {"kind": "context", **request})
            assert error.value.status_code == status and error.value.detail["code"] == code

    asyncio.run(check())


def test_imported_context_snapshot_survives_source_removal_and_detects_corruption():
    original = [{"role": "user", "content": "frozen body", "id": "message"}]
    entries, sources = freeze_authoring_sources(original, {"kind": "context", "context_id": "context", "context_hash": "old"})
    original[0]["content"] = "source advanced"

    class NoDatabaseReads:
        async def get(self, *_args):
            raise AssertionError("frozen Context sources do not need the original Context or Revision")

    async def check():
        reader = SourceAvailabilityReader(SimpleNamespace())
        status = await reader.inspect(NoDatabaseReads(), sources)
        assert status[entries[0].source_ref]["status"] == "available"
        assert sources[entries[0].source_ref]["record"]["content"] == "frozen body"
        corrupted = deepcopy(sources)
        corrupted[entries[0].source_ref]["record"]["content"] = "changed frozen body"
        status = await reader.inspect(NoDatabaseReads(), corrupted)
        assert status[entries[0].source_ref]["status"] == "unavailable"

    asyncio.run(check())
