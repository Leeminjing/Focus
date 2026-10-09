"""本文件对外提供工作区 Patrol 的追加 upgrade/downgrade。

输入为现有 Loop/Mission schema，输出为交互归属、工作区唯一约束与可缺省用户要求的 schema。
具体工作流为保留旧行，新增模式及来源，允许缺省 outcome；存在工作区 Patrol 时拒绝破坏性回退。
示例：alembic upgrade head；downgrade 仅限不存在新模式数据的隔离库。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f69c0d1e2f3a"
down_revision = "e58b9c0d1e2f"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "interaction_mode" not in {
        column["name"] for column in inspector.get_columns("agent_loops")
    }:
        op.add_column(
            "agent_loops",
            sa.Column(
                "interaction_mode", sa.String(24), nullable=False, server_default="context_loop"
            ),
        )
    if "ck_loop_interaction_mode" not in {
        check["name"] for check in inspector.get_check_constraints("agent_loops")
    }:
        op.create_check_constraint(
            "ck_loop_interaction_mode",
            "agent_loops",
            "interaction_mode IN ('context_loop','workspace_patrol')",
        )
    if "uq_workspace_patrol_active" not in {
        index["name"] for index in inspector.get_indexes("agent_loops")
    }:
        op.create_index(
            "uq_workspace_patrol_active",
            "agent_loops",
            ["workspace_id"],
            unique=True,
            postgresql_where=sa.text(
                "interaction_mode = 'workspace_patrol' AND status NOT IN ('completed','stopped','failed')"
            ),
        )
    op.alter_column("loop_mission_revisions", "outcome", existing_type=sa.Text(), nullable=True)
    if "input_sources" not in {
        column["name"] for column in inspector.get_columns("loop_mission_revisions")
    }:
        op.add_column(
            "loop_mission_revisions",
            sa.Column("input_sources", postgresql.JSONB(), nullable=False, server_default="{}"),
        )
    op.drop_index("uq_loop_wait_request_active", table_name="loop_wait_requests")
    op.create_index(
        "uq_loop_wait_request_active",
        "loop_wait_requests",
        ["loop_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('open','resolving') AND COALESCE(scope->>'information_only','false') <> 'true'"
        ),
    )
    if "uq_loop_information_request_active" not in {
        index["name"] for index in inspector.get_indexes("loop_wait_requests")
    }:
        op.create_index(
            "uq_loop_information_request_active",
            "loop_wait_requests",
            ["loop_id"],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('open','resolving') AND scope->>'information_only' = 'true'"
            ),
        )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM agent_loops WHERE interaction_mode = 'workspace_patrol')"
        )
    ):
        raise RuntimeError("工作区 Patrol 数据存在，不能删除模式、缺省 Mission 和用户来源")
    op.drop_index("uq_loop_information_request_active", table_name="loop_wait_requests")
    op.drop_index("uq_loop_wait_request_active", table_name="loop_wait_requests")
    op.create_index(
        "uq_loop_wait_request_active",
        "loop_wait_requests",
        ["loop_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open','resolving')"),
    )
    op.drop_column("loop_mission_revisions", "input_sources")
    op.alter_column("loop_mission_revisions", "outcome", existing_type=sa.Text(), nullable=False)
    op.drop_index("uq_workspace_patrol_active", table_name="agent_loops")
    op.drop_constraint("ck_loop_interaction_mode", "agent_loops", type_="check")
    op.drop_column("agent_loops", "interaction_mode")
