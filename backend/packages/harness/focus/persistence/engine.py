"""
本文件对外提供 init_engine、dispose_engine、get_session 三个公开函数，
管理 SQLAlchemy AsyncEngine 和 async_sessionmaker 进程级全局单例。

对外提供:
    init_engine(config: AppConfig) → AsyncEngine
        创建（或返回已有）AsyncEngine 全局单例，同时初始化 session factory；
        内部从 config.database 提取 DatabaseConfig

    dispose_engine() → Awaitable[None]
        异步释放 AsyncEngine 连接池，重置全局单例为 None

    get_session() → AsyncSession
        通过全局 session factory 创建新的 AsyncSession 实例，
        调用方通过 `async with get_session() as session:` 使用

输入:
    init_engine:
        config: AppConfig — 应用配置对象，其 database 字段包含数据库连接参数

输出:
    init_engine → AsyncEngine 实例（全局单例，幂等）

具体工作流:
    init_engine:
    (1) 检查全局 _engine 是否已存在，存在则直接返回
    (2) 校验 config.database 非空
    (3) 从 config.database 提取 url、echo、pool_size 等参数
    (4) 调用 create_async_engine() 创建 AsyncEngine
    (5) 调用 async_sessionmaker(engine, expire_on_commit=False) 创建 session factory
    (6) 存入模块级全局变量，返回 engine

    get_session:
    (1) 检查 _session_factory 是否已初始化，未初始化抛 RuntimeError
    (2) 通过 factory() 创建新 AsyncSession 并返回

示例:
    from focus.persistence.engine import init_engine, dispose_engine, get_session
    from focus.config import get_app_config

    app_config = get_app_config("config.yaml")
    engine = init_engine(app_config)

    async with get_session() as session:
        result = await session.execute(...)

    await dispose_engine()
"""

import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from focus.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _init_session_factory() -> None:
    """内部辅助：基于全局 _engine 创建 async_sessionmaker。

    输入: 无（读取全局 _engine）
    输出: None（写入全局 _session_factory）

    工作流:
        (1) 校验 _engine 非空
        (2) 调用 async_sessionmaker() 创建工厂，expire_on_commit=False
        (3) 写入 _session_factory
    """
    global _session_factory

    if _engine is None:
        raise RuntimeError("Engine 未初始化，请先调用 init_engine()")

    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
    )
    logger.info("session factory 已创建")


def init_engine(config: AppConfig) -> AsyncEngine:
    """创建（或返回已有）AsyncEngine 全局单例。

    输入:
        config: AppConfig — 应用配置对象，内部从 config.database 提取数据库连接参数

    输出:
        AsyncEngine — 全局单例 engine 实例

    工作流:
        (1) 若 _engine 已存在，直接返回（幂等）
        (2) 校验 config.database 非空
        (3) 从 config.database 提取 create_async_engine 所需参数
        (4) 调用 create_async_engine() 创建实例
        (5) 存入 _engine，调用 _init_session_factory()
        (6) 返回 engine

    示例:
        engine = init_engine(app_config)
        engine = init_engine(app_config)  # 幂等，直接返回已有实例
    """
    global _engine

    if _engine is not None:
        logger.info("AsyncEngine 已存在，直接返回")
        return _engine

    if config.database is None:
        raise ValueError("AppConfig.database 为空，无法初始化数据库引擎")

    db = config.database
    _engine = create_async_engine(
        db.url,
        echo=db.echo,
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        pool_timeout=db.pool_timeout,
        pool_pre_ping=db.pool_pre_ping,
        pool_recycle=db.pool_recycle,
        isolation_level=db.isolation_level,
    )
    logger.info("AsyncEngine 已创建 (backend=%s)", db.backend)

    _init_session_factory()
    return _engine


async def dispose_engine() -> None:
    """异步释放 AsyncEngine 连接池，重置全局单例为 None。

    输入: 无
    输出: None

    工作流:
        (1) 先清空全局引用，阻止关闭期间继续创建 session
        (2) 若原 _engine 非空，等待 engine.dispose() 释放连接池

    示例:
        await dispose_engine()
    """
    global _engine, _session_factory

    engine = _engine
    _engine = None
    _session_factory = None

    if engine is not None:
        await engine.dispose()
        logger.info("AsyncEngine 已释放")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回全局 session factory，供需要持有会话工厂的组件（如 DesktopService）使用。

    输入: 无
    输出: async_sessionmaker[AsyncSession] — 全局 session factory

    工作流:
        (1) 校验 _session_factory 已初始化
        (2) 返回 _session_factory
    """
    if _session_factory is None:
        raise RuntimeError("session factory 未初始化，请先调用 init_engine()")

    return _session_factory


def get_session() -> AsyncSession:
    """通过全局 session factory 创建新的 AsyncSession 实例。

    输入: 无
    输出: AsyncSession — 新创建的异步数据库会话

    工作流:
        (1) 校验 _session_factory 已初始化
        (2) 调用 factory() 创建并返回 AsyncSession

    示例:
        async with get_session() as session:
            result = await session.execute(select(...))
    """
    if _session_factory is None:
        raise RuntimeError("session factory 未初始化，请先调用 init_engine()")

    return _session_factory()
