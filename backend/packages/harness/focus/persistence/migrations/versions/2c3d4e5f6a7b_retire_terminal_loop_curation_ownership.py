r"""本文件对外提供终态 Loop 遗留策展所有权的数据修复迁移。

输入为 revision `1b2c3d4e5f6a` 中已有的 AgentLoop、CurationProgram 与 CurationLane 关系；输出为仅将可证明属于 completed/stopped/failed Loop 的非 retired managed Lane 标记 retired。
具体工作流为按 Loop.program_id 与 Lane.program_id 的持久关系执行幂等 UPDATE，不删除或重建任何历史 identity；downgrade 保留安全退休结果，避免重新制造多个发布者。
示例：`alembic upgrade 2c3d4e5f6a7b`。
"""

from alembic import op


revision = "2c3d4e5f6a7b"
down_revision = "1b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE curation_lanes AS lane
        SET lifecycle = 'retired',
            publisher_epoch = lane.publisher_epoch + 1,
            updated_at = now()
        FROM agent_loops AS loop
        WHERE loop.program_id = lane.program_id
          AND loop.status IN ('completed', 'stopped', 'failed')
          AND lane.managed_context_id IS NOT NULL
          AND lane.lifecycle IN ('active', 'paused')
        """
    )


def downgrade() -> None:
    pass
