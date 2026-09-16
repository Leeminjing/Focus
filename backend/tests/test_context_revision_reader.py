r"""本文件验证统一 Context revision reader 对迁移 root、derived 与 managed 语义的稳定投影。

输入为 migration-origin revision 合同与精确 checkpoint fixture；输出为 authored、execution、
display、checkpoint、historical、frontier 和 deleted-source 视图断言。具体工作流为用只读内存仓储
模拟已迁移记录并以假 checkpointer 返回冻结消息。示例：`pytest backend/tests/test_context_revision_reader.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.app.desktop.context_evolution import (
    ContextFrontierSummary,
    ContextRevisionContract,
    ContextRevisionHistoricalView,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionSourceContract,
    DeletedContextSourceView,
)


class _RevisionStore:
    def __init__(self, *revisions: ContextRevisionContract) -> None:
        self._revisions = {item.ref.revision_id: item for item in revisions}
        self._current = {item.ref.context_id: item for item in revisions}

    async def get(self, session: Any, ref: ContextRevisionRef) -> ContextRevisionContract:
        revision = self._revisions[ref.revision_id]
        assert revision.ref == ref
        return revision

    async def current(self, session: Any, context_id: str) -> ContextRevisionContract | None:
        return self._current.get(context_id)


class _Checkpointer:
    def __init__(self, messages: dict[str, list[Any]]) -> None:
        self._messages = messages

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        checkpoint_id = config["configurable"]["checkpoint_id"]
        if checkpoint_id not in self._messages:
            return None
        return SimpleNamespace(
            config={"configurable": {**config["configurable"]}},
            checkpoint={"channel_values": {"messages": self._messages[checkpoint_id]}},
            metadata={"source": "reader-fixture"},
        )


def _ref(
    context_id: str,
    revision_id: str,
    mode: ContextRevisionPayloadMode,
) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=1,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}",
        payload_mode=mode,
    )


def _revision(
    ref: ContextRevisionRef,
    *,
    authored: tuple[dict[str, Any], ...] = (),
    execution: tuple[dict[str, Any], ...] = (),
    initial_ids: tuple[str, ...] = (),
    sources: tuple[ContextRevisionSourceContract, ...] = (),
    deleted: bool = False,
) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        sources=sources,
        authored_messages=authored,
        execution_messages=execution,
        initial_message_ids=initial_ids,
        definition_hash="d" * 64 if ref.payload_mode is ContextRevisionPayloadMode.DEFINITION else None,
        projection_hash="p" * 64 if ref.payload_mode is ContextRevisionPayloadMode.DEFINITION else None,
        content_hash=(ref.revision_id[-1] * 64),
        projection_status=(
            ContextRevisionProjectionStatus.DELETED
            if deleted
            else ContextRevisionProjectionStatus.VALID
        ),
        origin_kind=ContextRevisionOriginKind.MIGRATION,
        origin_id=ref.context_id,
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
        deleted_at=datetime(2026, 9, 14, tzinfo=UTC) if deleted else None,
    )


@pytest.mark.parametrize("kind", ["derived", "managed"])
def test_migrated_definition_display_preserves_authored_prefix_and_runtime_suffix(kind: str) -> None:
    async def run() -> None:
        ref = _ref(f"{kind}-context", f"{kind}-revision-1", ContextRevisionPayloadMode.DEFINITION)
        authored = ({"id": f"{kind}-authored", "role": "human", "content": f"{kind} 用户定义"},)
        execution = ({"id": f"{kind}-execution", "role": "human", "content": f"{kind} 执行投影"},)
        revision = _revision(ref, authored=authored, execution=execution, initial_ids=(f"{kind}-execution",))
        reader = ContextRevisionReader(
            _RevisionStore(revision),
            _Checkpointer(
                {
                    ref.checkpoint_id: [
                        HumanMessage(id=f"{kind}-execution", content=f"{kind} 执行投影"),
                        AIMessage(id=f"{kind}-answer", content=f"{kind} 后续结果"),
                    ]
                }
            ),
        )

        authored_view = await reader.read(None, ref, "authored")
        execution_view = await reader.read(None, ref, "execution")
        display_view = await reader.read(None, ref, "display")

        assert authored_view.messages == authored
        assert [item["id"] for item in execution_view.messages] == [
            f"{kind}-execution",
            f"{kind}-answer",
        ]
        assert [item["id"] for item in display_view.messages] == [
            f"{kind}-authored",
            f"{kind}-answer",
        ]

    asyncio.run(run())


def test_checkpoint_backed_root_and_historical_views_use_exact_checkpoint() -> None:
    async def run() -> None:
        ref = _ref("root-context", "root-revision-1", ContextRevisionPayloadMode.CHECKPOINT)
        revision = _revision(ref)
        reader = ContextRevisionReader(
            _RevisionStore(revision),
            _Checkpointer(
                {
                    ref.checkpoint_id: [
                        HumanMessage(id="root-human", content="原始目标"),
                        AIMessage(id="root-ai", content="已完成数据库层"),
                    ]
                }
            ),
        )

        current = await reader.read_current(None, ref.context_id, "display")
        historical = await reader.read(None, ref, "historical")

        assert [item["id"] for item in current.messages] == ["root-human", "root-ai"]
        assert isinstance(historical, ContextRevisionHistoricalView)
        assert historical.authored.messages == historical.execution.messages
        assert historical.checkpoint.ref == ref
        assert historical.checkpoint.metadata == {"source": "reader-fixture"}

    asyncio.run(run())


def test_frontier_is_bounded_and_deleted_source_history_remains_visible() -> None:
    async def run() -> None:
        deleted_ref = _ref("old-context", "old-revision-1", ContextRevisionPayloadMode.CHECKPOINT)
        target_ref = _ref("target-context", "target-revision-1", ContextRevisionPayloadMode.DEFINITION)
        deleted = _revision(deleted_ref, deleted=True)
        target = _revision(
            target_ref,
            authored=({"id": "target-human", "role": "human", "content": "x" * 400},),
            execution=({"id": "target-execution", "role": "human", "content": "x" * 400},),
            initial_ids=("target-execution",),
            sources=(ContextRevisionSourceContract(source=deleted_ref, position=0),),
        )
        reader = ContextRevisionReader(
            _RevisionStore(deleted, target),
            _Checkpointer(
                {
                    deleted_ref.checkpoint_id: [HumanMessage(id="old", content="旧来源")],
                    target_ref.checkpoint_id: [HumanMessage(id="target-execution", content="x" * 400)],
                }
            ),
        )

        frontier = await reader.read(None, target_ref, "frontier-summary")
        deleted_sources = await reader.read(None, target_ref, "deleted-source")

        assert isinstance(frontier, ContextFrontierSummary)
        assert len(frontier.summary) == 280
        assert frontier.source_frontier == (deleted_ref,)
        assert isinstance(deleted_sources, DeletedContextSourceView)
        assert deleted_sources.sources[0].deleted is True
        assert deleted_sources.sources[0].source == deleted_ref

    asyncio.run(run())
