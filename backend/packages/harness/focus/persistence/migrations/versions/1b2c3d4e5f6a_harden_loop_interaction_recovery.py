r"""本文件对外提供 Loop 等待、后继激活、Run 调度与投影恢复的可逆持久化迁移。

输入为 materialized Loop facts revision `0a1b2c3d4e5f`；输出为 additive activation lineage、wait、dispatch 与 projection 表。
具体工作流为按引用顺序创建独立授权 lineage、请求/响应、调度与投影审计，再把旧 waiting Loop 和无 dispatch Run 分类为可操作恢复状态；downgrade 仅反序移除本版本结构。
示例：`alembic upgrade 1b2c3d4e5f6a`。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "1b2c3d4e5f6a"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_activations",
        sa.Column("activation_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("selected_run_id", sa.String(length=32), nullable=False),
        sa.Column("predecessor_loop_id", sa.String(length=32), nullable=True),
        sa.Column("readiness_token", sa.String(length=64), nullable=False),
        sa.Column("activation_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["selected_run_id"], ["desktop_runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["predecessor_loop_id"], ["agent_loops.loop_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("activation_id"),
        sa.UniqueConstraint("loop_id", name="uq_loop_activation_loop"),
        sa.UniqueConstraint("activation_key", name="uq_loop_activation_key"),
        sa.UniqueConstraint("selected_run_id", name="uq_loop_activation_selected_run"),
    )
    op.create_index("ix_loop_activations_loop_id", "loop_activations", ["loop_id"])
    op.create_index("ix_loop_activations_selected_run_id", "loop_activations", ["selected_run_id"])
    op.create_index("ix_loop_activations_predecessor_loop_id", "loop_activations", ["predecessor_loop_id"])

    op.create_table(
        "loop_wait_requests",
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response_mode", sa.String(length=24), nullable=False),
        sa.Column("response_contract", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column("causation_id", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open','resolving','resolved','cancelled','superseded')", name="ck_loop_wait_request_status"),
        sa.CheckConstraint("response_mode IN ('text','single_choice','multiple_choice','structured','action')", name="ck_loop_wait_request_response_mode"),
        sa.CheckConstraint("revision > 0", name="ck_loop_wait_request_revision"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_loop_wait_requests_loop_id", "loop_wait_requests", ["loop_id"])
    op.create_index("ix_loop_wait_requests_round_id", "loop_wait_requests", ["round_id"])
    op.create_index(
        "uq_loop_wait_request_active",
        "loop_wait_requests",
        ["loop_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open','resolving')"),
    )
    op.create_table(
        "loop_wait_responses",
        sa.Column("response_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("request_revision > 0", name="ck_loop_wait_response_revision"),
        sa.ForeignKeyConstraint(["request_id"], ["loop_wait_requests.request_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("response_id"),
        sa.UniqueConstraint("request_id", name="uq_loop_wait_response_request"),
        sa.UniqueConstraint("idempotency_key", name="uq_loop_wait_response_idempotency"),
    )
    op.create_index("ix_loop_wait_responses_request_id", "loop_wait_responses", ["request_id"])

    op.create_table(
        "run_dispatches",
        sa.Column("dispatch_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="accepted", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_by", sa.String(length=120), nullable=True),
        sa.Column("fencing_token", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("running_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('accepted','claimed','running','failed_to_start','interrupted','settled')", name="ck_run_dispatch_status"),
        sa.CheckConstraint("attempt >= 0 AND fencing_token >= 0", name="ck_run_dispatch_counters"),
        sa.CheckConstraint("status NOT IN ('claimed','running') OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL AND attempt > 0 AND fencing_token > 0)", name="ck_run_dispatch_claim_identity"),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("dispatch_id"),
        sa.UniqueConstraint("run_id", name="uq_run_dispatch_run"),
    )
    op.create_index("ix_run_dispatches_run_id", "run_dispatches", ["run_id"])
    op.create_index(
        "uq_run_dispatch_active_claim",
        "run_dispatches",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('claimed','running')"),
    )

    op.create_table(
        "loop_projection_failures",
        sa.Column("failure_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("projector_name", sa.String(length=80), nullable=False),
        sa.Column("unit_kind", sa.String(length=32), nullable=False),
        sa.Column("unit_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=120), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("boundary_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('retryable','quarantined','resolved')", name="ck_loop_projection_failure_status"),
        sa.CheckConstraint("attempt_count > 0", name="ck_loop_projection_failure_attempts"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("failure_id"),
        sa.UniqueConstraint("loop_id", "projector_name", "unit_kind", "unit_id", name="uq_loop_projection_failure_unit"),
    )
    op.create_index("ix_loop_projection_failures_loop_id", "loop_projection_failures", ["loop_id"])
    op.create_table(
        "loop_projection_unit_outcomes",
        sa.Column("outcome_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("projector_name", sa.String(length=80), nullable=False),
        sa.Column("unit_kind", sa.String(length=32), nullable=False),
        sa.Column("unit_id", sa.String(length=160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("boundary_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('succeeded','retryable','quarantined')", name="ck_loop_projection_outcome_status"),
        sa.CheckConstraint("attempt > 0", name="ck_loop_projection_outcome_attempt"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint("loop_id", "projector_name", "unit_kind", "unit_id", "attempt", name="uq_loop_projection_outcome_attempt"),
    )
    op.create_index("ix_loop_projection_unit_outcomes_loop_id", "loop_projection_unit_outcomes", ["loop_id"])

    op.execute(
        sa.text(
            """
            INSERT INTO loop_wait_requests (
                request_id, loop_id, round_id, kind, prompt, response_mode,
                response_contract, scope, status, revision, correlation_id,
                causation_id, created_by, created_at, updated_at
            )
            SELECT
                md5(loop_id || '\\:legacy-wait'),
                loop_id,
                current_round_id,
                'legacy_recovery',
                COALESCE(NULLIF(waiting_reason, ''), '旧版本 Loop 正在等待用户决定，请选择重试或停止。'),
                'action',
                '{"actions":[{"action":"retry","label":"重试"},{"action":"stop","label":"停止 Loop"}]}'::jsonb,
                jsonb_build_object('legacy', true, 'original_status', status),
                'open',
                1,
                'legacy-recovery\\:' || loop_id,
                NULL,
                'legacy_migration',
                COALESCE(created_at, now()),
                now()
            FROM agent_loops
            WHERE status = 'waiting_user'
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO run_dispatches (
                dispatch_id, run_id, status, attempt, claimed_by, fencing_token,
                lease_expires_at, error, accepted_at, settled_at, updated_at
            )
            SELECT
                md5(run_id || '\\:legacy-dispatch'),
                run_id,
                CASE
                    WHEN status = 'pending'
                     AND kind = 'main'
                     AND COALESCE(equipment, '{}'::jsonb) ? '_durable_dispatch_execution'
                    THEN 'accepted'
                    ELSE 'interrupted'
                END,
                0,
                NULL,
                0,
                NULL,
                CASE
                    WHEN status = 'pending'
                     AND kind = 'main'
                     AND COALESCE(equipment, '{}'::jsonb) ? '_durable_dispatch_execution'
                    THEN NULL
                    ELSE '旧版本运行缺少可重建的 durable execution 或可证明的 dispatch ownership，已安全中断；原输入保留，可显式重试。'
                END,
                COALESCE(created_at, now()),
                CASE
                    WHEN status = 'pending'
                     AND kind = 'main'
                     AND COALESCE(equipment, '{}'::jsonb) ? '_durable_dispatch_execution'
                    THEN NULL
                    ELSE now()
                END,
                now()
            FROM desktop_runs
            WHERE status IN ('pending', 'running')
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE desktop_runs
            SET status = 'interrupted',
                error = COALESCE(error, '旧版本运行在重启时缺少可证明的 dispatch ownership，已安全中断；原输入保留，可显式重试。'),
                settled_at = COALESCE(settled_at, now())
            WHERE status IN ('pending', 'running')
              AND EXISTS (
                  SELECT 1 FROM run_dispatches rd
                  WHERE rd.run_id = desktop_runs.run_id AND rd.status = 'interrupted'
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_loop_projection_unit_outcomes_loop_id", table_name="loop_projection_unit_outcomes")
    op.drop_table("loop_projection_unit_outcomes")
    op.drop_index("ix_loop_projection_failures_loop_id", table_name="loop_projection_failures")
    op.drop_table("loop_projection_failures")
    op.drop_index("uq_run_dispatch_active_claim", table_name="run_dispatches")
    op.drop_index("ix_run_dispatches_run_id", table_name="run_dispatches")
    op.drop_table("run_dispatches")
    op.drop_index("ix_loop_wait_responses_request_id", table_name="loop_wait_responses")
    op.drop_table("loop_wait_responses")
    op.drop_index("uq_loop_wait_request_active", table_name="loop_wait_requests")
    op.drop_index("ix_loop_wait_requests_round_id", table_name="loop_wait_requests")
    op.drop_index("ix_loop_wait_requests_loop_id", table_name="loop_wait_requests")
    op.drop_table("loop_wait_requests")
    op.drop_index("ix_loop_activations_predecessor_loop_id", table_name="loop_activations")
    op.drop_index("ix_loop_activations_selected_run_id", table_name="loop_activations")
    op.drop_index("ix_loop_activations_loop_id", table_name="loop_activations")
    op.drop_table("loop_activations")
