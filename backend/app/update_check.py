"""Read-only release acceptance: schema, Gateway lifespan and desktop reads.

Run with the same Python/environment as Electron. No model calls or background
work is dispatched. Startup migrations belong to the managed update transaction.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys


async def check_schema() -> None:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    import backend.app.desktop.persistence_registry  # noqa: F401
    from focus.persistence.base import Base

    ini = Path(__file__).resolve().parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"
    config = Config(str(ini))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            actual = await connection.run_sync(lambda conn: set(MigrationContext.configure(conn).get_current_heads()))
            if actual != expected:
                raise RuntimeError("Database schema does not match this release. Run 'focus update'.")
            # Read every mapped column, including Context, Revision and history,
            # so a revision stamp alone cannot hide a broken migration.
            for table in Base.metadata.sorted_tables:
                await connection.execute(select(table).limit(0))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(check_schema())
    if "--schema-only" in sys.argv:
        return
    os.environ["FOCUS_UPDATE_CHECK"] = "1"
    from fastapi.testclient import TestClient
    from backend.app.gateway.app import app

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/health")
        response.raise_for_status()
        if response.json() != {"status": "ok"}:
            raise RuntimeError("Gateway health response is invalid")
        headers = {"X-Focus-Session": app.state.session_key}
        response = client.get("/desktop/api/tasks", headers=headers)
        response.raise_for_status()
        client.get("/desktop/", headers=headers).raise_for_status()
    print("Focus schema, Gateway and desktop read checks passed.")


if __name__ == "__main__":
    main()
