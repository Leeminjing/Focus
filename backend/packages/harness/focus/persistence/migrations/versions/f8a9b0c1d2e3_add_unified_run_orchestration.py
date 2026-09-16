r"""本文件对外提供统一 Run identity、活跃执行约束与 MainRunSettled outbox 的可逆迁移。

输入为 Atomic Portfolio Publication revision `e6f7a8b9c0d1`；输出为 Run origin/Context/Loop/equipment/
workspace/settlement 字段、活跃 main 与 idempotency 唯一索引，以及 Run outbox/receipt 表。具体工作流为
先扩展 legacy runs，再建立并发约束和 durable event 事实。
示例：`alembic upgrade f8a9b0c1d2e3`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "desktop_runs",
        sa.Column("origin", sa.String(length=32), server_default="direct_user", nullable=False),
    )
    op.add_column("desktop_runs", sa.Column("execution_thread_id", sa.String(length=128)))
    op.add_column(
        "desktop_runs", sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False)
    )
    op.add_column("desktop_runs", sa.Column("context_revision_id", sa.String(length=32)))
    op.add_column("desktop_runs", sa.Column("context_checkpoint_id", sa.Text()))
    op.add_column("desktop_runs", sa.Column("origin_message_id", sa.String(length=64)))
    op.add_column("desktop_runs", sa.Column("directive_id", sa.String(length=32)))
    op.add_column("desktop_runs", sa.Column("loop_id", sa.String(length=32)))
    op.add_column("desktop_runs", sa.Column("round_id", sa.String(length=32)))
    op.add_column("desktop_runs", sa.Column("action_id", sa.String(length=32)))
    op.add_column(
        "desktop_runs",
        sa.Column(
            "equipment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "desktop_runs",
        sa.Column(
            "workspace_anchor",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("desktop_runs", sa.Column("idempotency_key", sa.String(length=160)))
    op.add_column("desktop_runs", sa.Column("final_checkpoint_id", sa.Text()))
    op.add_column(
        "desktop_runs",
        sa.Column(
            "workspace_result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("desktop_runs", sa.Column("settled_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_desktop_run_context_revision",
        "desktop_runs",
        "desktop_context_revisions",
        ["context_revision_id"],
        ["revision_id"],
        ondelete="RESTRICT",
    )
    for column in ("context_revision_id", "directive_id", "loop_id", "round_id", "action_id"):
        op.create_index(f"ix_desktop_runs_{column}", "desktop_runs", [column])
    op.create_index(
        "uq_desktop_active_main_execution",
        "desktop_runs",
        ["task_id", "execution_thread_id", "checkpoint_ns"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'main' AND status IN ('pending', 'running') "
            "AND execution_thread_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_desktop_run_idempotency_key",
        "desktop_runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "run_outbox_events",
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("delivery_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_by", sa.String(length=120)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'error')",
            name="ck_run_outbox_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "event_type", name="uq_run_outbox_result"),
    )
    op.create_index("ix_run_outbox_events_run_id", "run_outbox_events", ["run_id"])
    op.create_table(
        "run_outbox_deliveries",
        sa.Column("delivery_id", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", sa.String(length=120), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["run_outbox_events.event_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("event_id", "consumer_id", name="uq_run_outbox_delivery"),
    )
    op.create_index(
        "ix_run_outbox_deliveries_event_id", "run_outbox_deliveries", ["event_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_run_outbox_deliveries_event_id", table_name="run_outbox_deliveries")
    op.drop_table("run_outbox_deliveries")
    op.drop_index("ix_run_outbox_events_run_id", table_name="run_outbox_events")
    op.drop_table("run_outbox_events")
    op.drop_index("uq_desktop_run_idempotency_key", table_name="desktop_runs")
    op.drop_index("uq_desktop_active_main_execution", table_name="desktop_runs")
    for column in reversed(("context_revision_id", "directive_id", "loop_id", "round_id", "action_id")):
        op.drop_index(f"ix_desktop_runs_{column}", table_name="desktop_runs")
    op.drop_constraint("fk_desktop_run_context_revision", "desktop_runs", type_="foreignkey")
    for column in reversed(
        (
            "origin",
            "execution_thread_id",
            "checkpoint_ns",
            "context_revision_id",
            "context_checkpoint_id",
            "origin_message_id",
            "directive_id",
            "loop_id",
            "round_id",
            "action_id",
            "equipment",
            "workspace_anchor",
            "idempotency_key",
            "final_checkpoint_id",
            "workspace_result",
            "settled_at",
        )
    ):
        op.drop_column("desktop_runs", column)
