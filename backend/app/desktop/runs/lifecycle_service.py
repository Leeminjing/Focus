"""RunLifecycleService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from focus.runtime.runs.manager import RunRecord

from backend.app.desktop.contexts.evolution import ContextRevisionOriginKind
from backend.app.desktop.runs.policy import (
    _RUN_FINALIZATION_RETRY_INITIAL_SECONDS,
    _RUN_FINALIZATION_RETRY_MAX_SECONDS,
)

logger = logging.getLogger(__name__)


class RunLifecycleServicePorts(Protocol):
    _closing: Any

    _pending_run_finalizations: Any
    _run_dispatch_repository: Any
    _run_sync_tasks: Any
    _set_run_status: Any

    _sync_tasks: Any

    context_patrol: Any
    contexts: Any

    def notify_run_dispatch(self) -> None: ...

    run_lifecycle: Any
    session_factory: Any


class RunLifecycleService:
    def __init__(self, ports: RunLifecycleServicePorts) -> None:
        self._ports = ports

    def attach_run_sync(self, record: RunRecord) -> None:
        ports = self._ports
        if ports._closing:
            return
        existing = ports._run_sync_tasks.get(record.run_id)
        if existing is not None and (not existing.done()):
            return
        ports._pending_run_finalizations[record.run_id] = record
        task = asyncio.create_task(
            self._sync_run_status(record), name=f"run-finalize:{record.run_id}"
        )
        ports._run_sync_tasks[record.run_id] = task
        ports._sync_tasks.add(task)
        task.add_done_callback(ports._sync_tasks.discard)

    def _resume_pending_run_finalizations(self) -> None:
        ports = self._ports
        for record in tuple(ports._pending_run_finalizations.values()):
            self.attach_run_sync(record)

    async def _sync_run_status(self, record: RunRecord) -> None:
        ports = self._ports
        try:
            try:
                await asyncio.shield(record.task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                logger.exception(
                    "Run 执行任务异常退出，继续收尾: run_id=%s", record.run_id
                )
            activity_task = getattr(record, "loop_activity_task", None)
            if activity_task is not None:
                try:
                    await asyncio.shield(activity_task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    logger.warning(
                        "Loop Run 活动事件提交已取消: run_id=%s", record.run_id
                    )
                except Exception:
                    logger.exception(
                        "Loop Run 活动事件提交失败: run_id=%s", record.run_id
                    )
            delay = _RUN_FINALIZATION_RETRY_INITIAL_SECONDS
            while True:
                try:
                    await self._finalize_run_status(record)
                except Exception:
                    logger.exception(
                        "收敛 Run 终态失败，%s 秒后重试: run_id=%s",
                        delay,
                        record.run_id,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _RUN_FINALIZATION_RETRY_MAX_SECONDS)
                else:
                    getattr(ports, "_pending_run_finalizations", {}).pop(
                        record.run_id, None
                    )
                    return
        finally:
            task = asyncio.current_task()
            ports._sync_tasks.discard(task)
            tasks = getattr(ports, "_run_sync_tasks", {})
            if tasks.get(record.run_id) is task:
                tasks.pop(record.run_id, None)

    async def _finalize_run_status(self, record: RunRecord) -> None:
        ports = self._ports
        if not hasattr(ports, "run_lifecycle") and hasattr(ports, "_set_run_status"):
            kind, task_id = await ports._set_run_status(
                record.run_id,
                record.status.value,
                record.error,
                record.prompt_input_tokens,
                record.prompt_cache_hit_tokens,
            )
            if kind == "main":
                await ports.contexts.evolution.publish_latest_checkpoint(
                    task_id,
                    ContextRevisionOriginKind.RUN_SETTLED,
                    origin_id=record.run_id,
                )
                await ports.context_patrol.notify_stable_context_checkpoint(task_id)
            return
        settlement = await ports.run_lifecycle.finalize(record)
        async with ports.session_factory.begin() as session:
            await ports._run_dispatch_repository.settle_by_run(session, record.run_id)
        ports.notify_run_dispatch()
        if settlement.kind == "main":
            try:
                await ports.context_patrol.notify_stable_context_checkpoint(
                    settlement.task_id
                )
            except Exception:
                logger.warning(
                    "登记稳定 Context checkpoint 失败: context_id=%s",
                    settlement.task_id,
                    exc_info=True,
                )
