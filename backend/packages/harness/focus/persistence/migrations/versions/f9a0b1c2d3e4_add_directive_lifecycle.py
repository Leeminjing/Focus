r"""本迁移对外提供 Directive/User Intervention current lifecycle 与不可变 transition history。

输入为既有 loop_directives；输出为 revision/origin/correlation/causation/terminal 字段和 transition 表。具体工作流为
追加兼容列并从 round/actor/status 回填 current state，之后创建历史表；downgrade 反序移除。示例：`alembic upgrade head`。
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS delivery_state VARCHAR(24) DEFAULT 'submitted' NOT NULL")
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS origin_kind VARCHAR(24) DEFAULT 'user' NOT NULL")
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(120)")
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS resulting_run_id VARCHAR(32)")
    op.execute("ALTER TABLE loop_user_intents ADD COLUMN IF NOT EXISTS terminal_reason TEXT")
    op.execute("UPDATE loop_user_intents SET correlation_id = intent_id WHERE correlation_id IS NULL")
    op.alter_column("loop_user_intents", "correlation_id", nullable=False)
    op.execute("ALTER TABLE desktop_runs ADD COLUMN IF NOT EXISTS user_intent_id VARCHAR(32)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_desktop_runs_user_intent_id ON desktop_runs (user_intent_id)")
    op.create_table(
        "loop_intervention_transitions",
        sa.Column("transition_id", sa.String(length=32), nullable=False),
        sa.Column("intent_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["loop_user_intents.intent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint("intent_id", "revision", name="uq_loop_intervention_transition_revision"),
    )
    op.create_index("ix_loop_intervention_transitions_intent_id", "loop_intervention_transitions", ["intent_id"])
    op.create_index("ix_loop_intervention_transitions_loop_id", "loop_intervention_transitions", ["loop_id"])
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(24) DEFAULT 'proposed' NOT NULL")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS origin_kind VARCHAR(24) DEFAULT 'patrol' NOT NULL")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(120)")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS causation_event_id VARCHAR(120)")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS terminal_reason TEXT")
    op.execute("UPDATE loop_directives SET correlation_id = round_id WHERE correlation_id IS NULL")
    op.execute("UPDATE loop_directives SET lifecycle_state = CASE status WHEN 'created' THEN 'authorized' WHEN 'launching' THEN 'delivering' WHEN 'launched' THEN 'run_started' WHEN 'blocked' THEN 'delivery_failed' WHEN 'cancelled' THEN 'cancelled' ELSE 'proposed' END")
    op.execute("UPDATE loop_directives SET revision = 1")
    op.alter_column("loop_directives", "correlation_id", nullable=False)
    op.create_table(
        "loop_directive_transitions",
        sa.Column("transition_id", sa.String(length=32), nullable=False),
        sa.Column("directive_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("caused_by_event_id", sa.String(length=120), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["directive_id"], ["loop_directives.directive_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint("directive_id", "revision", name="uq_loop_directive_transition_revision"),
    )
    op.create_index("ix_loop_directive_transitions_directive_id", "loop_directive_transitions", ["directive_id"])
    op.create_index("ix_loop_directive_transitions_loop_id", "loop_directive_transitions", ["loop_id"])


def downgrade() -> None:
    op.drop_index("ix_loop_directive_transitions_loop_id", table_name="loop_directive_transitions")
    op.drop_index("ix_loop_directive_transitions_directive_id", table_name="loop_directive_transitions")
    op.drop_table("loop_directive_transitions")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS terminal_reason")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS causation_event_id")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS correlation_id")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS origin_kind")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS revision")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS lifecycle_state")
    op.drop_index("ix_loop_intervention_transitions_loop_id", table_name="loop_intervention_transitions")
    op.drop_index("ix_loop_intervention_transitions_intent_id", table_name="loop_intervention_transitions")
    op.drop_table("loop_intervention_transitions")
    op.execute("DROP INDEX IF EXISTS ix_desktop_runs_user_intent_id")
    op.execute("ALTER TABLE desktop_runs DROP COLUMN IF EXISTS user_intent_id")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS terminal_reason")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS resulting_run_id")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS correlation_id")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS origin_kind")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS revision")
    op.execute("ALTER TABLE loop_user_intents DROP COLUMN IF EXISTS delivery_state")
