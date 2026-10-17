"""Lightweight, payload-free attention projection for task lists and notifications.

New settlements persist this projection in workspace_result. Older interrupted runs
are read using their execution identity, without loading task details or messages.
"""
from typing import Any

from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from backend.app.desktop.pending_interrupts import pending_interrupt_payload


async def read_task_attention(checkpointer: Any, run: Any, task: Any, checkpoint: Any = None, *, status: str | None = None) -> list[dict]:
    if (status or run.status) != "interrupted":
        return []
    thread_id = run.execution_thread_id or task.thread_id
    if checkpoint is None:
        checkpoint = await checkpointer.aget_tuple({"configurable": {
            "thread_id": thread_id, "checkpoint_ns": run.checkpoint_ns or "",
        }})
    items = []
    for payload_type, kind in (
        ("access_review", "access_approval"),
        ("compression_request", "compression"),
        ("must_view_report", "must_view"),
    ):
        if pending_interrupt_payload(checkpoint, payload_type) is not None:
            items.append({"id": f"{run.run_id}:{kind}", "kind": kind, "run_id": run.run_id})
    commitment = await checkpointer.aget_tuple({"configurable": {
        "thread_id": commitment_subgraph_thread_id(thread_id),
    }})
    values = getattr(commitment, "checkpoint", {}).get("channel_values", {})
    try:
        stage = int(values.get("awaiting_human") or 0)
    except (TypeError, ValueError):
        stage = 0
    if 1 <= stage <= 9:
        items.append({"id": f"{run.run_id}:commitment:{stage}", "kind": "commitment", "run_id": run.run_id})
    return items
