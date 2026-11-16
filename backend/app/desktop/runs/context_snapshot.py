"""Self-contained, integrity-checked Context input owned by one Run.

The typed source is retained verbatim, including provider continuation data.
Execution reads and resume never substitute a newer Context for this input.
"""

from copy import deepcopy

from focus.history import content_hash

from backend.app.desktop.contexts.evolution import ContextRevisionContract


async def capture_context_snapshot(session, reader, revision):
    views = {}
    for kind in ("authored", "execution"):
        view = await reader.read(session, revision.ref, kind)
        views[kind] = list(view.messages)
    payload = {
        "schema_version": "run-context-input-v1",
        "revision": revision.model_dump(mode="json"),
        "views": views,
    }
    return {**payload, "snapshot_hash": content_hash(payload)}


def read_context_snapshot(raw):
    payload = {key: value for key, value in raw.items() if key != "snapshot_hash"}
    if payload.get("schema_version") != "run-context-input-v1" or content_hash(payload) != raw.get("snapshot_hash"):
        raise ValueError("Run Context input snapshot integrity mismatch")
    revision = ContextRevisionContract.model_validate(payload["revision"])
    return revision, deepcopy(payload["views"])


def context_snapshot_for_run(run):
    raw = (getattr(run, "equipment", None) or {}).get("context_snapshot")
    if raw is None:
        return None
    revision, views = read_context_snapshot(raw)
    ref = revision.ref
    if (ref.context_id, ref.revision_id, ref.execution_thread_id, ref.checkpoint_ns, ref.checkpoint_id) != (
        run.task_id, run.context_revision_id, run.execution_thread_id, run.checkpoint_ns or "", run.context_checkpoint_id,
    ):
        raise ValueError("Run source does not match its frozen Context input")
    return revision, views
