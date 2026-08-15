"""
本文件对外提供 RunRecord 数据类和 RunManager 运行管理器。

对外提供:
    RunRecord — 单次 run 的运行时档案（dataclass）
    RunManager — 进程内 run 状态管理注册表

输入:
    RunManager.__init__(store: RunStore | None) → 初始化管理器实例

输出:
    RunManager — 管理所有 run 状态的内存注册表

具体工作流:
    RunManager 维护 self._runs: dict[str, RunRecord] 作为权威数据源，
    可选挂载 RunStore 做 best-effort 持久化。store 操作失败只打日志，
    不影响主流程。

    RunManager 提供以下核心能力:
        - create(thread_id, ...) → RunRecord: 生成 UUID、创建 RunRecord、登记并持久化
        - get(run_id) → RunRecord | None: 按 ID 查询单个 run
        - list_by_thread(thread_id, user_id=None) → list[RunRecord]: 按 thread 列出
        - update(run_id, **fields) → bool: 更新字段 + 刷新 updated_at + store 同步
        - cancel(run_id, action) → bool: 软硬双通道取消（abort_event.set + task.cancel）
        - _cleanup_later(run_id): 延迟 5 分钟后从内存删除，不删 store

    cancel 返回值语义:
        返回 True  — 取消成功或幂等（已 interrupted）
        返回 False — run 不存在或已到达终态（success/error/timeout）

示例:
    from focus.runtime.runs.manager import RunRecord, RunManager
    from focus.runtime.runs.schemas import RunStatus, DisconnectMode
    from focus.runtime.runs.store.memory import MemoryRunStore

    store = MemoryRunStore()
    mgr = RunManager(store=store)
    record = mgr.create(thread_id="th-001", on_disconnect=DisconnectMode.continue_)
    # ...
    mgr.cancel(record.run_id, action="interrupt")
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from focus.runtime.runs.schemas import DisconnectMode, RunStatus

logger = logging.getLogger(__name__)

_CLEANUP_DELAY_SECONDS = 300  # 5 分钟


@dataclass
class RunRecord:
    """单次 run 的运行时档案。

    既包含元数据，也持有运行时控制对象（task、abort_event），
    使 RunManager 能查询、更新、取消这个 run。
    """

    # === 身份信息 ===
    run_id: str
    thread_id: str

    # === 状态信息 ===
    status: RunStatus
    on_disconnect: DisconnectMode

    # === 时间戳（ISO 8601 字符串）===
    created_at: str = ""
    updated_at: str = ""

    # === 运行时控制对象（不参与序列化，不打印到日志）===
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"

    # === 结果信息 ===
    error: str | None = None
    model_name: str | None = None
    prompt_input_tokens: int = 0
    prompt_cache_hit_tokens: int = 0

    # === 特殊标志 ===
    store_only: bool = False


class RunManager:
    """管理所有 run 状态的内存注册表，可选挂载 RunStore 做长期保存。"""

    def __init__(self, store=None) -> None:
        """初始化 RunManager。

        输入:
            store: RunStore | None — 可选，持久化存储后端。为 None 时仅内存管理

        输出:
            RunManager 实例

        示例:
            mgr = RunManager()
            mgr = RunManager(store=MemoryRunStore())
        """
        self._runs: dict[str, RunRecord] = {}
        self._store = store

    # === helpers ===

    @staticmethod
    def _generate_run_id() -> str:
        """生成唯一的 run_id（UUID4 字符串）。"""
        return uuid.uuid4().hex

    @staticmethod
    def _now_iso() -> str:
        """返回当前 UTC 时间的 ISO 8601 字符串。"""
        return datetime.now(timezone.utc).isoformat()

    def _persist_put(self, record: RunRecord) -> None:
        """best-effort 将 RunRecord 写入 store。"""
        if self._store is None:
            return
        try:
            self._store.put(
                record.run_id,
                thread_id=record.thread_id,
                status=record.status.value,
                on_disconnect=record.on_disconnect.value,
                created_at=record.created_at,
                updated_at=record.updated_at,
                abort_action=record.abort_action,
                error=record.error,
                model_name=record.model_name,
            )
        except Exception:
            logger.error(
                "store.put(run_id='%s') 失败，已跳过（best-effort）",
                record.run_id,
                exc_info=True,
            )

    def _persist_status(self, run_id: str, status: RunStatus, error: str | None = None) -> None:
        """best-effort 将状态变更写入 store。"""
        if self._store is None:
            return
        try:
            self._store.update_status(run_id, status.value, error=error)
        except Exception:
            logger.error(
                "store.update_status(run_id='%s') 失败，已跳过（best-effort）",
                run_id,
                exc_info=True,
            )

    async def _do_cleanup(self, run_id: str) -> None:
        """延迟后从内存删除 run，不删 store。"""
        await asyncio.sleep(_CLEANUP_DELAY_SECONDS)
        removed = self._runs.pop(run_id, None)
        if removed is not None:
            logger.info("run '%s' 已从内存清理", run_id)

    # === 核心操作 ===

    def create(self, thread_id: str, run_id: str | None = None, **fields) -> RunRecord:
        """创建一条新的 run 记录。

        输入:
            thread_id: str — 所属会话线程 ID
            run_id: str | None — 显式 run_id（如桌面 DB 记录 ID），None 时自动生成
            **fields — 可选字段，可传入 on_disconnect、model_name 等

        输出:
            RunRecord — 新创建的 run 记录，初始状态 pending

        示例:
            record = mgr.create("th-001")
            record = mgr.create("th-001", on_disconnect=DisconnectMode.continue_)
            record = mgr.create("th-001", run_id="db-run-1")
        """
        now = self._now_iso()
        on_disconnect = fields.pop("on_disconnect", DisconnectMode.cancel)
        model_name = fields.pop("model_name", None)

        record = RunRecord(
            run_id=run_id or self._generate_run_id(),
            thread_id=thread_id,
            status=RunStatus.pending,
            on_disconnect=on_disconnect,
            created_at=now,
            updated_at=now,
            model_name=model_name,
        )

        self._runs[record.run_id] = record
        self._persist_put(record)
        logger.info("run '%s' 已创建 (thread='%s', status=pending)", record.run_id, thread_id)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        """按 run_id 查询 run 记录。

        输入:
            run_id: str — run 的唯一标识

        输出:
            RunRecord | None — 命中返回记录，未命中返回 None

        示例:
            record = mgr.get("abc123")
        """
        return self._runs.get(run_id)

    def list_by_thread(
        self, thread_id: str, user_id: str | None = None
    ) -> list[RunRecord]:
        """按 thread_id 查询该 thread 下的所有 run。

        输入:
            thread_id: str — 线程标识
            user_id: str | None — 可选，权限过滤

        输出:
            list[RunRecord] — 该 thread 下的所有 run 记录

        示例:
            runs = mgr.list_by_thread("th-001")
        """
        result: list[RunRecord] = []
        for record in self._runs.values():
            if record.thread_id != thread_id:
                continue
            if user_id is not None and getattr(record, "user_id", None) != user_id:
                continue
            result.append(record)
        return result

    def update(self, run_id: str, **fields) -> bool:
        """更新 run 记录的指定字段。

        输入:
            run_id: str — run 的唯一标识
            **fields — 要更新的字段名和值

        输出:
            bool — 命中并更新返回 True，未命中返回 False

        示例:
            mgr.update("abc123", model_name="deepseek-v4-flash")
        """
        record = self._runs.get(run_id)
        if record is None:
            logger.warning("RunManager.update: run_id '%s' 不存在", run_id)
            return False

        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)

        record.updated_at = self._now_iso()
        return True

    def cancel(self, run_id: str, action: str = "interrupt") -> bool:
        """终止一个正在执行或等待执行的 run。

        使用软信号（abort_event.set）和硬取消（task.cancel）双管齐下。

        输入:
            run_id: str — 要取消的 run ID
            action: str — 取消方式，"interrupt" 保留 checkpoint，"rollback" 回滚

        输出:
            bool — 取消成功/幂等返回 True，不存在/终态返回 False

        具体工作流:
            (1) run 不存在 → 返回 False
            (2) status == interrupted → 幂等，返回 True
            (3) status 不是 pending/running → 终态，返回 False
            (4) status 是 pending/running → 执行取消:
                - 设置 abort_action = action
                - abort_event.set() 拉响软信号
                - task 未结束则 task.cancel() 硬取消
                - status = interrupted，刷新 updated_at
                - 持久化状态 → 返回 True

        示例:
            mgr.cancel("abc123")
            mgr.cancel("abc123", action="rollback")
        """
        record = self._runs.get(run_id)
        if record is None:
            logger.warning("RunManager.cancel: run_id '%s' 不存在", run_id)
            return False

        if record.status == RunStatus.interrupted:
            return True

        if record.status not in (RunStatus.pending, RunStatus.running):
            logger.info(
                "RunManager.cancel: run_id '%s' 已达终态 status=%s，无法取消",
                run_id,
                record.status.value,
            )
            return False

        record.abort_action = action
        record.abort_event.set()

        if record.task is not None and not record.task.done():
            record.task.cancel()

        record.status = RunStatus.interrupted
        record.updated_at = self._now_iso()

        self._persist_status(run_id, RunStatus.interrupted, error=None)
        logger.info("run '%s' 已取消 (action=%s)", run_id, action)
        return True

    def _cleanup_later(self, run_id: str) -> None:
        """安排延迟清理任务。

        run 结束后延迟 5 分钟从内存删除，给晚到的 SSE 订阅者留时间。
        不删除 store 中的持久化记录。

        输入:
            run_id: str — 要清理的 run ID

        输出:
            None — 返回后由后台 task 执行延迟删除
        """
        if run_id not in self._runs:
            return

        async def _delayed_cleanup() -> None:
            await self._do_cleanup(run_id)

        try:
            asyncio.create_task(_delayed_cleanup())
        except RuntimeError:
            pass
