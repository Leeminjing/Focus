"""Run inputs remain exact after Context replacement, without historical rows."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from focus.history import FocusItem, HistoryPayload

from backend.app.desktop.contexts.evolution import (
    ContextRevisionContract,
    ContextRevisionReader,
    ContextRevisionRef,
)
from backend.app.desktop.runs.context_snapshot import (
    capture_context_snapshot,
    context_snapshot_for_run,
)
from backend.app.desktop.runs.orchestration.execution_history import (
    repaired_execution_for_run,
)
from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer


def _revision():
    history = HistoryPayload(execution_items=(FocusItem(
        item_id="old-message", kind="message", origin="provider",
        payload={"message": {"id": "old-message", "role": "assistant", "content": "source",
                             "provider_continuation": {"encrypted_content": "opaque-source"}}},
    ),))
    return ContextRevisionContract(
        ref=ContextRevisionRef(context_id="context", revision_id="source", generation=1,
                               execution_thread_id="execution", checkpoint_ns="ns", checkpoint_id="base",
                               payload_mode="checkpoint"),
        history_payload=history, content_hash="a" * 64, projection_status="repaired",
        origin_kind="run_settled", created_at=datetime.now(UTC),
    )


async def _frozen_run():
    revision = _revision()

    class Repository:
        async def get(self, session, ref):
            assert ref == revision.ref
            return revision

    snapshot = await capture_context_snapshot(None, ContextRevisionReader(Repository(), None), revision)
    return SimpleNamespace(
        kind="main", task_id="context", context_revision_id="source", execution_thread_id="execution",
        checkpoint_ns="ns", context_checkpoint_id="base", equipment={"context_snapshot": snapshot},
    )


def test_repaired_run_uses_its_frozen_input_without_accessing_current_or_checkpoints(monkeypatch):
    async def check():
        run = await _frozen_run()

        def forbidden():
            raise AssertionError("frozen Run must not look up Context state")

        monkeypatch.setattr("backend.app.desktop.runs.orchestration.execution_history.ContextRevisionRepository", forbidden)
        messages = await repaired_execution_for_run(None, run, None)
        assert messages[0]["content"] == "source"
        assert messages[0]["provider_continuation"] == {"encrypted_content": "opaque-source"}
        revision, _ = context_snapshot_for_run(run)
        assert revision.history_payload == _revision().history_payload
        messages[0]["content"] = "mutated result"
        assert context_snapshot_for_run(run)[1]["execution"][0]["content"] == "source"

    asyncio.run(check())


def test_changed_frozen_body_is_rejected_before_execution():
    run = asyncio.run(_frozen_run())
    run.equipment["context_snapshot"]["views"]["execution"][0]["content"] = "new context"
    with pytest.raises(ValueError, match="integrity"):
        context_snapshot_for_run(run)


@pytest.mark.parametrize("field,value", [("context_revision_id", "new-source"), ("context_checkpoint_id", "new-base")])
def test_frozen_input_cannot_be_rebound_to_another_execution(field, value):
    run = asyncio.run(_frozen_run())
    setattr(run, field, value)
    with pytest.raises(ValueError, match="does not match"):
        context_snapshot_for_run(run)


def test_context_input_lives_only_until_execution_or_pending_resume_is_finished():
    async def check():
        released = []

        async def release(session, *, owner_kind, owner_id):
            released.append((owner_kind, owner_id))

        run = await _frozen_run()
        run.run_id, run.status = "pending-resume", "interrupted"
        run.equipment["model_name"] = "model"
        run.equipment["resume_context_snapshot"] = run.equipment["context_snapshot"]
        finalizer = RunLifecycleFinalizer(None, None, context_revisions=SimpleNamespace(release=release))
        await finalizer._release_context_input(None, run)
        assert run.equipment.get("resume_context_snapshot") and released == []
        run.status = "success"
        await finalizer._release_context_input(None, run)
        assert run.equipment == {"model_name": "model"}
        assert released == [("run", "pending-resume")]

    asyncio.run(check())
