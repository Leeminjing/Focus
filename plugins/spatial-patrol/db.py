"""spatial-patrol 插件的数据库自管理:经系统全局 session factory 建表。

对外提供:
    ensure_tables() — 插件接入时调用,CREATE TABLE IF NOT EXISTS spatial_anchors

插件自治原则:表结构由插件自己负责,不进入系统 Alembic 迁移链;卸载时表与数据保留。
"""

import logging

logger = logging.getLogger(__name__)


async def ensure_tables() -> None:
    """用插件 ORM 元数据在全局数据库上建表(仅本插件的表)。"""
    from focus.persistence.engine import get_session_factory

    from plugins.spatial_patrol.models import SpatialAnchor

    factory = get_session_factory()
    async with factory() as session:
        await session.run_sync(
            lambda sync_session: SpatialAnchor.__table__.create(
                sync_session.connection(), checkfirst=True,
            )
        )
        # PostgreSQL DDL 参与当前事务;显式提交,避免 session 关闭时回滚建表。
        await session.commit()
    logger.info("spatial-patrol: spatial_anchors 表已就绪")
