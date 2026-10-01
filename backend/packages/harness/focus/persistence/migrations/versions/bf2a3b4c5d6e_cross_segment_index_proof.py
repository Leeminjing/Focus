"""本文件对外提供完整跨段索引 proof 的 additive 数据库约束及回滚边界。

输入为已有局部索引和预算预留 schema；输出为新版 rev 合同必须含已完成综合 proof 的约束。
工作流为升级仅添加 JSONB CHECK，不改旧 payload；降级删除该新合同索引再撤销约束，避免旧 records 表删除后遗留新引用。
示例：alembic upgrade head；停止 writers 后 downgrade 保留 v3/v4、Context 和任务记忆，受撤销影响的新版 planning refs 需重规划。
"""

import sqlalchemy as sa
from alembic import op

revision = "bf2a3b4c5d6e"
down_revision = "ae1f2a3b4c5d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_check_constraint(
        "ck_loop_semantic_interpretation_complete",
        "loop_semantic_index_artifacts",
        "projector_version NOT LIKE 'rev:%' OR COALESCE(jsonb_typeof(payload -> 'interpretation') = 'object' AND (payload -> 'interpretation' ->> 'completed') = 'true', false)",
    )


def downgrade():
    op.get_bind().execute(
        sa.text(
            "DELETE FROM loop_semantic_index_artifacts WHERE projector_version LIKE 'rev:%'"
        )
    )
    op.drop_constraint(
        "ck_loop_semantic_interpretation_complete",
        "loop_semantic_index_artifacts",
        type_="check",
    )
