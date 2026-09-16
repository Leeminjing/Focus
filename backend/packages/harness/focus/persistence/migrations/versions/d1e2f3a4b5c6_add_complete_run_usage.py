r"""本文件对外提供 Desktop Run 完整模型用量字段的可逆数据库迁移。

输入为已包含统一 Run identity 的数据库；输出为每个 Run 的模型调用数与输出 token 持久字段。
具体工作流为添加非空零默认列，使旧 Run 可安全回填为未知零值，downgrade 仅移除新增列。
示例：`alembic upgrade d1e2f3a4b5c6`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "desktop_runs",
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "desktop_runs",
        sa.Column("prompt_output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("desktop_runs", "prompt_output_tokens")
    op.drop_column("desktop_runs", "model_call_count")
