"""只读检查实际数据库的 Alembic heads；不加载迁移 env.py 或 Gateway。

连接失败返回 unavailable，让桌面宿主按需启动本地 Compose；查询/脚本错误应失败，
不能被当作健康状态，也不能用本地版本缓存跳过数据库核对。
"""
import asyncio
import json
import os
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def revision_status(connection, heads):
    current = set(MigrationContext.configure(connection).get_current_heads())
    return "current" if current == set(heads) else "pending"


async def database_status(url, config):
    heads = ScriptDirectory.from_config(config).get_heads()
    engine = create_async_engine(url)
    try:
        try:
            connection = await asyncio.wait_for(engine.connect(), timeout=3)
        except (OSError, TimeoutError):
            return "unavailable"
        try:
            await connection.execute(text("SELECT 1"))
            return await connection.run_sync(revision_status, heads)
        finally:
            await connection.close()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    config = Config(str(Path(__file__).resolve().parents[2] / "backend/packages/harness/focus/persistence/migrations/alembic.ini"))
    print(json.dumps({"status": asyncio.run(database_status(os.environ["FOCUS_DATABASE_URL"], config))}))
