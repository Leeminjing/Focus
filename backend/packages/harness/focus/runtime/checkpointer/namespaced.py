"""
本文件对外提供 NamespacedCheckpointer 类，将根 LangGraph 图固定到一个显式 checkpoint 命名空间。

对外提供:
    NamespacedCheckpointer(BaseCheckpointSaver) — 装饰器式包装，把所有 checkpoint 读写映射到指定命名空间

输入:
    NamespacedCheckpointer.__init__(backend, namespace):
        backend: BaseCheckpointSaver — 底层 checkpoint 持久化器
        namespace: str — 目标命名空间（如 "patrol:{agent_id}"），"" 表示根命名空间

输出:
    NamespacedCheckpointer — 与 backend 同接口的包装实例，对外表现为根命名空间

具体工作流:
    (1) _stored: 将调用方 config 的 checkpoint_ns 替换为目标命名空间后转发底层
    (2) _root: 将底层返回 config 的 checkpoint_ns 还原为 ""（对调用方隐藏命名空间）
    (3) 所有读写方法（aget_tuple/alist/aput/aput_writes 等）经 _stored/_root 对称映射
    (4) 小兵与主 Agent 共用 thread_id 时，以不同 namespace 隔离 checkpoint

示例:
    saver = NamespacedCheckpointer(postgres_saver, "patrol:abc123")
    tuple_ = await saver.aget_tuple({"configurable": {"thread_id": "th-1"}})
"""

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple


class NamespacedCheckpointer(BaseCheckpointSaver):
    """Keep a root LangGraph in one explicit PostgreSQL checkpoint namespace."""

    def __init__(self, backend: BaseCheckpointSaver, namespace: str) -> None:
        super().__init__(serde=backend.serde)
        self.backend = backend
        self.namespace = namespace

    @property
    def config_specs(self):
        return self.backend.config_specs

    def _stored(self, config):
        result = {**config, "configurable": {**config.get("configurable", {})}}
        result["configurable"]["checkpoint_ns"] = self.namespace
        return result

    @staticmethod
    def _root(config):
        if config is None:
            return None
        result = {**config, "configurable": {**config.get("configurable", {})}}
        result["configurable"]["checkpoint_ns"] = ""
        return result

    def _root_tuple(self, value):
        if value is None:
            return None
        return CheckpointTuple(
            self._root(value.config), value.checkpoint, value.metadata,
            self._root(value.parent_config), value.pending_writes,
        )

    async def aget_tuple(self, config):
        return self._root_tuple(await self.backend.aget_tuple(self._stored(config)))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        stored = self._stored(config) if config is not None else None
        stored_before = self._stored(before) if before is not None else None
        async for value in self.backend.alist(
            stored, filter=filter, before=stored_before, limit=limit
        ):
            yield self._root_tuple(value)

    async def aput(self, config, checkpoint, metadata, new_versions):
        saved = await self.backend.aput(
            self._stored(config), checkpoint, metadata, new_versions
        )
        return self._root(saved)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await self.backend.aput_writes(
            self._stored(config), writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id):
        await self.backend.adelete_thread(thread_id)

    async def adelete_for_runs(self, run_ids):
        await self.backend.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id, target_thread_id):
        await self.backend.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(self, thread_ids, *, strategy="keep_latest"):
        await self.backend.aprune(thread_ids, strategy=strategy)

    async def aget_delta_channel_history(self, *, config, channels):
        return await self.backend.aget_delta_channel_history(
            config=self._stored(config), channels=channels
        )

    def get_next_version(self, current, channel):
        return self.backend.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist):
        return NamespacedCheckpointer(
            self.backend.with_allowlist(extra_allowlist), self.namespace
        )
