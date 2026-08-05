"""
本文件对外提供 create_checkpointer 异步工厂函数，基于 AppConfig 创建 BaseCheckpointSaver 实例。

对外提供:
    create_checkpointer(config: AppConfig) -> BaseCheckpointSaver

输入:
    config: AppConfig — 应用配置对象，内部从 config.checkpointer 提取 CheckpointerConfig

输出:
    BaseCheckpointSaver — PostgresSaver 或 InMemorySaver 实例

具体工作流:
    (1) 读取 config.checkpointer.type
    (2) type == "postgres" → 从 config.database.url 创建 asyncpg 连接
        → 创建 PostgresSaver(conn) 实例并返回；不调用 setup()（表已由 Alembic 迁移管理）
    (3) type == "memory" → 创建 InMemorySaver 实例并返回
    (4) 其他 → 抛 ValueError

示例:
    from focus.runtime.checkpointer import create_checkpointer
    from focus.config import get_app_config

    app_config = get_app_config("config.yaml")
    checkpointer = await create_checkpointer(app_config)
"""

import logging

import asyncpg
from langgraph.checkpoint.base import BaseCheckpointSaver

from focus.config.app_config import AppConfig

logger = logging.getLogger(__name__)


async def create_checkpointer(config: AppConfig) -> BaseCheckpointSaver:
    checkpointer_type = config.checkpointer.type

    if checkpointer_type == "postgres":
        if config.database is None:
            raise RuntimeError("AppConfig.database 为空，无法创建 PostgresSaver")

        # ponytail: 去掉 SQLAlchemy 的 +asyncpg 驱动前缀
        dsn = config.database.url.replace("postgresql+asyncpg://", "postgresql://")
        # 必须使用 aio 变体：worker 与 NamespacedCheckpointer 全部走 async 方法
        # （aget_tuple/aput），sync PostgresSaver 无 async 实现，会回落 base 抛 NotImplementedError。
        # v3 的 AsyncPostgresSaver 使用 psycopg 连接，镜像官方 from_conn_string 的建连方式
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        conn = await AsyncConnection.connect(
            dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = AsyncPostgresSaver(conn)
        # ponytail: 不调用 setup()，表结构已由 Alembic 迁移管理
        logger.info("AsyncPostgresSaver 已创建 (url=%s)", config.database.url)
        return checkpointer

    if checkpointer_type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("InMemorySaver 已创建")
        return InMemorySaver()

    raise ValueError(f"不支持的 checkpointer 类型: '{checkpointer_type}'，仅支持 'postgres' 或 'memory'")


async def dispose_checkpointer(checkpointer: BaseCheckpointSaver) -> None:
    """释放 checkpointer 持有的资源。

    输入:
        checkpointer: BaseCheckpointSaver — 要释放的 checkpointer 实例

    输出:
        None

    具体工作流:
        (1) 若为 PostgresSaver → 关闭底层 asyncpg 连接
        (2) 若为 InMemorySaver → 无需操作
    """
    from langgraph.checkpoint.memory import InMemorySaver

    if isinstance(checkpointer, InMemorySaver):
        return

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    if isinstance(checkpointer, (PostgresSaver, AsyncPostgresSaver)):
        # sync 类用 _conn，aio 类用 conn；兼容两种实例
        conn = getattr(checkpointer, "_conn", None) or getattr(checkpointer, "conn", None)
        if conn is not None:
            await conn.close()
            logger.info("PostgresSaver asyncpg 连接已关闭")
