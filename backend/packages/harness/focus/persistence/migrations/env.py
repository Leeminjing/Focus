"""
Alembic 迁移环境配置。

本文件由 alembic init 自动生成骨架，在此基础上修改：
  - 绑定 target_metadata = Base.metadata 以支持 autogenerate
  - 仅保留 online 异步迁移路径（run_migrations_online），删除 offline 分支
  - 使用 create_async_engine 建立异步数据库连接

输入: 无（Alembic CLI 自动加载并执行）
输出: 无（通过 context.run_migrations() 执行迁移）

具体工作流:
    asyncio.run(run_migrations_online()):
        (1) 从 alembic.ini 读取 sqlalchemy.url
        (2) 调用 create_async_engine(url) 创建异步引擎
        (3) async with engine.connect() 获取连接
        (4) 调用 connection.run_sync(do_run_migrations) 在同步上下文中执行迁移
        (5) 执行完毕后 dispose 引擎

    do_run_migrations(connection):
        (1) context.configure(connection, target_metadata) 注册连接和表结构
        (2) with context.begin_transaction() 开启事务
        (3) context.run_migrations() 执行 versions/ 中的迁移脚本

示例:
    cd backend/packages/harness/focus/persistence/migrations
    alembic revision --autogenerate -m "create users table"
    alembic upgrade head
"""

import asyncio
from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from focus.persistence.base import Base

# Alembic Config 对象
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 支持：绑定声明式基类的 metadata
target_metadata = Base.metadata


def do_run_migrations(connection):
    """在同步连接上执行迁移。

    输入:
        connection: 同步数据库连接（由 run_sync 提供）
    输出:
        None
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    """以异步 online 模式执行迁移。

    输入: 无（从 alembic.ini 读取配置）
    输出: None
    """
    url = os.getenv("FOCUS_DATABASE_URL", config.get_main_option("sqlalchemy.url"))

    engine = create_async_engine(url)

    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await engine.dispose()


asyncio.run(run_migrations_online())
