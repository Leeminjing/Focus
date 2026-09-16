r"""本文件对外提供 Atomic Portfolio Publication 持久化结构的可逆数据库迁移。

输入为多 Context 策展迁移 `d5e6f7a8b9c0`；输出为冻结控制/Lane 快照、candidate fencing、
publication attempt 与幂等 outbox 表。具体工作流为扩展 Portfolio 聚合后创建发布尝试和投递去重
事实，使 shadow prepare 与权威 pointer switch 可在重启后区分和恢复。
示例：`alembic upgrade e6f7a8b9c0d1`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "curation_portfolio_revisions",
        sa.Column(
            "control_revisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "curation_portfolio_revisions",
        sa.Column(
            "target_lanes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "curation_portfolio_lane_candidates",
        sa.Column("target_context_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "curation_portfolio_lane_candidates",
        sa.Column("base_publisher_epoch", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "curation_portfolio_lane_candidates",
        sa.Column(
            "message_lineage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "curation_portfolio_lane_candidates",
        sa.Column(
            "source_dispositions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_curation_candidate_base_publisher_epoch_positive",
        "curation_portfolio_lane_candidates",
        "base_publisher_epoch > 0",
    )
    op.create_table(
        "curation_portfolio_publication_attempts",
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("portfolio_revision_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_curation_publication_attempt_positive",
        ),
        sa.CheckConstraint(
            "status IN ('preparing', 'ready', 'publishing', 'published', 'error', "
            "'superseded', 'recovery_required')",
            name="ck_curation_publication_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_revision_id"],
            ["curation_portfolio_revisions.portfolio_revision_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "portfolio_revision_id",
            "attempt_number",
            name="uq_curation_publication_attempt_number",
        ),
    )
    op.create_index(
        "ix_cur_pub_attempt_portfolio",
        "curation_portfolio_publication_attempts",
        ["portfolio_revision_id"],
    )
    op.create_table(
        "curation_outbox_events",
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("delivery_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_by", sa.String(length=120), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'error')",
            name="ck_curation_outbox_status",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "aggregate_id", "event_type", name="uq_curation_outbox_aggregate_event"
        ),
    )
    op.create_index(
        "ix_curation_outbox_events_aggregate_id",
        "curation_outbox_events",
        ["aggregate_id"],
    )
    op.create_table(
        "curation_outbox_deliveries",
        sa.Column("delivery_id", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", sa.String(length=120), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["curation_outbox_events.event_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("event_id", "consumer_id", name="uq_curation_outbox_delivery"),
    )
    op.create_index(
        "ix_curation_outbox_deliveries_event_id",
        "curation_outbox_deliveries",
        ["event_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_curation_outbox_deliveries_event_id", table_name="curation_outbox_deliveries"
    )
    op.drop_table("curation_outbox_deliveries")
    op.drop_index("ix_curation_outbox_events_aggregate_id", table_name="curation_outbox_events")
    op.drop_table("curation_outbox_events")
    op.drop_index(
        "ix_cur_pub_attempt_portfolio",
        table_name="curation_portfolio_publication_attempts",
    )
    op.drop_table("curation_portfolio_publication_attempts")
    op.drop_constraint(
        "ck_curation_candidate_base_publisher_epoch_positive",
        "curation_portfolio_lane_candidates",
        type_="check",
    )
    op.drop_column("curation_portfolio_lane_candidates", "source_dispositions")
    op.drop_column("curation_portfolio_lane_candidates", "message_lineage")
    op.drop_column("curation_portfolio_lane_candidates", "base_publisher_epoch")
    op.drop_column("curation_portfolio_lane_candidates", "target_context_id")
    op.drop_column("curation_portfolio_revisions", "target_lanes")
    op.drop_column("curation_portfolio_revisions", "control_revisions")
