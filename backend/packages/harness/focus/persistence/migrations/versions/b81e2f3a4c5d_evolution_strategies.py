"""本文件对外提供演化策略记忆的增量迁移。

输入为旧 memory_items；输出为兼容普通记忆的分类字段及六个可编辑全局参考条目。
工作流仅添加缺失字段，以稳定身份幂等插入预置，不覆盖用户编辑；降级保留新增字段与文件事件索引，应用回滚不删除用户数据。
示例：alembic upgrade head。
同次迁移收窄 outbox 唯一索引的适用范围，让文件事件按稳定版本身份连续发布；旧事件唯一性不变。
"""

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

revision = "b81e2f3a4c5d"
down_revision = "a70d1e2f3b4c"
branch_labels = None
depends_on = None

_SEEDS = (
    ("diagnose", "诊断—重构", "用户否定已有方案，需要重新考虑。", "先让原 Context 分析失败原因，真实诊断发布后再规划重做 Context。", "原始要求、最新有效纠正、诊断和有效约束；旧方案只按失败证据需要选入。"),
    ("explore", "独立探索", "未确定的新想法需要独立考察。", "必要时派生探索 Context，不改变原工作归属。", "问题、背景、约束和相关证据，不继承无关过程。"),
    ("compete", "竞争性派生", "多个可行思路需要比较。", "从共同基线生成独立候选，按资源推进并比较真实结果。", "共同要求和评价条件，各分支必要材料，避免复制未验证结论。"),
    ("split", "任务拆分", "职责独立且依赖明确的子任务。", "按职责组织 Context，有依赖时等待真实前置结果，满足隔离才并行。", "各自任务、接口、约束及已确认前置结果。"),
    ("revise", "决策变更", "用户修订已确认决定。", "识别受影响工作，保留有效成果，继续或重新派生。", "新决定、修订关系和有效约束；被替代决定不再作为要求。"),
    ("combine", "综合汇合", "多个 Context 的成果需要整合。", "从真实 revision 提取有效结果，必要时生成综合 Context。", "有效成果、差异和未决问题，保留来源及不确定性。"),
)


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "uq_run_outbox_result" in {item["name"] for item in inspector.get_unique_constraints("run_outbox_events")}:
        op.drop_constraint("uq_run_outbox_result", "run_outbox_events", type_="unique")
    if "uq_run_outbox_result" not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("run_outbox_events")}:
        op.create_index("uq_run_outbox_result", "run_outbox_events", ["run_id", "event_type"], unique=True,
            postgresql_where=sa.text("event_type <> 'ArtifactVersion'"))
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("memory_items")}
    for column in (
        sa.Column("category", sa.String(24), nullable=False, server_default="memory"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"), nullable=True),
        sa.Column("strategy", JSONB(), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("memory_items", column)
    table = sa.table("memory_items", sa.column("memory_id"), sa.column("title"), sa.column("content"),
        sa.column("category"), sa.column("enabled"), sa.column("strategy", JSONB()), sa.column("source_kind"),
        sa.column("content_mode"), sa.column("segments", JSONB()), sa.column("source_snapshot", JSONB()))
    for key, title, applicability, evolution, inheritance in _SEEDS:
        op.execute(insert(table).values(memory_id=uuid.uuid5(uuid.NAMESPACE_URL, "focus:evolution:" + key).hex,
            title=title, content="", category="evolution_strategy", enabled=True, source_kind="preset",
            content_mode="complete", segments=[], source_snapshot=[], strategy={"applicability": applicability,
                "evolution": evolution, "inheritance": inheritance}).on_conflict_do_nothing(index_elements=["memory_id"]))


def downgrade():
    pass
