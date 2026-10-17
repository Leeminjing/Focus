"""Task-list attention uses exact execution identity and carries no private payload."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.app.desktop.task_attention import read_task_attention


def test_interrupted_attention_preserves_namespace_and_omits_payload():
    async def scenario():
        checkpoint = SimpleNamespace(pending_writes=[
            ("task", "__interrupt__", {"type": "access_review", "command": "private"}),
            ("task", "__interrupt__", {"type": "compression_request", "messages": ["private"]}),
        ])
        commitment = SimpleNamespace(checkpoint={"channel_values": {"awaiting_human": 3}})
        checkpointer = SimpleNamespace(aget_tuple=AsyncMock(side_effect=[checkpoint, commitment]))
        run = SimpleNamespace(run_id="run", status="interrupted", execution_thread_id="execution", checkpoint_ns="branch")
        task = SimpleNamespace(thread_id="root")
        items = await read_task_attention(checkpointer, run, task)
        assert [item["kind"] for item in items] == ["access_approval", "compression", "commitment"]
        assert all(set(item) == {"id", "kind", "run_id"} for item in items)
        assert checkpointer.aget_tuple.call_args_list[0].args[0] == {
            "configurable": {"thread_id": "execution", "checkpoint_ns": "branch"},
        }
        run.status = "success"
        assert await read_task_attention(checkpointer, run, task) == []
        assert checkpointer.aget_tuple.await_count == 2
    asyncio.run(scenario())


def test_settlement_projects_terminal_status_before_the_run_row_is_updated():
    async def scenario():
        checkpoint = SimpleNamespace(pending_writes=[("task", "__interrupt__", {"type": "access_review"})])
        checkpointer = SimpleNamespace(aget_tuple=AsyncMock(return_value=None))
        run = SimpleNamespace(run_id="run", status="running", execution_thread_id=None, checkpoint_ns="")
        items = await read_task_attention(checkpointer, run, SimpleNamespace(thread_id="root"), checkpoint, status="interrupted")
        assert items == [{"id": "run:access_approval", "kind": "access_approval", "run_id": "run"}]
        assert checkpointer.aget_tuple.await_count == 1
    asyncio.run(scenario())
