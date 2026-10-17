"""增加可分享策略包身份，保留已有正文、启停及工作区设置；同范围 ID 唯一。

普通自定义策略使用 local/<memory_id>；预置策略保留稳定官方身份和原始发布摘要。
降级保留安装元信息，避免回滚丢失用户策略身份。
"""

import hashlib
import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "d03a4b5c6e7f"
down_revision = "c92f3a4b5d6e"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if "strategy_package" not in {column["name"] for column in sa.inspect(connection).get_columns("memory_items")}:
        op.add_column("memory_items", sa.Column("strategy_package", JSONB(), nullable=True))
    table = sa.table("memory_items", sa.column("memory_id"), sa.column("category"), sa.column("title"),
                     sa.column("strategy", JSONB()), sa.column("strategy_package", JSONB()))
    from importlib import import_module
    seeds = import_module("focus.persistence.migrations.versions.b81e2f3a4c5d_evolution_strategies")._SEEDS
    official = {uuid.uuid5(uuid.NAMESPACE_URL, "focus:evolution:" + seed[0]).hex: seed for seed in seeds}
    missing_package = sa.or_(table.c.strategy_package.is_(None), table.c.strategy_package == sa.cast(sa.literal("null"), JSONB()))
    for row in connection.execute(sa.select(table).where(table.c.category == "evolution_strategy", missing_package)).mappings():
        seed = official.get(row["memory_id"])
        metadata = {"schema": "focus.strategy/v1", "id": f"focus/{seed[0]}" if seed else f"local/{row['memory_id']}",
                    "name": seed[1] if seed else row["title"], "description": seed[2] if seed else "", "version": "1.0.0"}
        strategy = dict(zip(("applicability", "evolution", "inheritance"), seed[2:])) if seed else row["strategy"]
        canonical = json.dumps({"metadata": metadata, "strategy": strategy}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        package = {**metadata, "name": row["title"], "installed_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
        connection.execute(table.update().where(table.c.memory_id == row["memory_id"]).values(strategy_package=package))
    if "uq_strategy_package_scope" not in {index["name"] for index in sa.inspect(connection).get_indexes("memory_items")}:
        op.create_index("uq_strategy_package_scope", "memory_items",
                        [sa.text("(strategy_package ->> 'id')"), sa.text("COALESCE(workspace_id, '')")], unique=True,
                        postgresql_where=sa.text("category = 'evolution_strategy' AND strategy_package IS NOT NULL"))


def downgrade():
    pass
