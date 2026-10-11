"""本文件对外提供同轮 Observation 版本、认知归属和串行进度 work 的迁移。

输入为旧轮次与不可变记忆数据，输出为每轮唯一成功授权、多观察版本及独立进度工作身份。
具体工作流为追加字段、根据旧可信身份回填、移除单轮单观察限制并建立新唯一约束；旧正文、费用和 receipts 保留。
同一历史 Context 的模式不一致时采用最小已证实权限；较高模式的原 Run 保留装备并写入冲突诊断，等待显式处理。
示例：alembic upgrade head；存在新版数据时拒绝破坏性 downgrade，不把多版本历史压成旧单行。
"""

import hashlib
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a70d1e2f3b4c"
down_revision = "f69c0d1e2f3a"
branch_labels = None
depends_on = None


def _column(table, column):
    if column.name not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
        op.add_column(table, column)


def _drop_unique(table, columns):
    for item in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if item["column_names"] == columns:
            op.drop_constraint(item["name"], table, type_="unique")


def _unique(table, name, columns):
    if name not in {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints(table)}:
        op.create_unique_constraint(name, table, columns)


def _index(table, name, columns, *, where=None, unique=False):
    if name not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}:
        options = {"postgresql_where": sa.text(where)} if where else {}
        op.create_index(name, table, columns, unique=unique, **options)


def upgrade():
    _column("loop_rounds", sa.Column("trigger_kind", sa.String(24), nullable=False, server_default="automatic"))
    _column("loop_observations", sa.Column("generation", sa.Integer(), nullable=False, server_default="1"))
    _drop_unique("loop_observations", ["round_id"])
    _unique("loop_observations", "uq_loop_observation_generation", ["round_id", "generation"])
    _drop_unique("loop_decision_inputs", ["round_id"])
    _index("loop_decision_inputs", "ix_loop_decision_inputs_round_id", ["round_id"])
    for table in ("loop_decisions", "loop_patrol_attempts", "loop_worker_requests"):
        _column(table, sa.Column("observation_id", sa.String(32), sa.ForeignKey("loop_observations.observation_id", ondelete="RESTRICT")))
        _column(table, sa.Column("observation_generation", sa.Integer(), nullable=False, server_default="1"))
        _index(table, f"ix_{table}_observation_id", ["observation_id"])
        op.execute(sa.text(f"UPDATE {table} x SET observation_id = r.observation_id FROM loop_rounds r WHERE x.round_id = r.round_id AND x.observation_id IS NULL"))
    _column("loop_patrol_attempts", sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"))
    _drop_unique("loop_patrol_attempts", ["round_id", "attempt"])
    _unique("loop_patrol_attempts", "uq_loop_patrol_attempt", ["observation_id", "attempt"])
    _column("loop_decisions", sa.Column("authority_committed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE loop_decisions SET authority_committed_at = created_at WHERE authority_committed_at IS NULL AND status IN ('committed','publishing','publishing_run','adopting')")
    op.execute("UPDATE loop_decisions d SET authority_committed_at = d.created_at FROM loop_rounds r WHERE r.decision_id = d.decision_id AND d.authority_committed_at IS NULL AND (EXISTS (SELECT 1 FROM loop_actions a WHERE a.decision_id = d.decision_id AND a.status IN ('committed','applied')) OR EXISTS (SELECT 1 FROM loop_directives x WHERE x.decision_id = d.decision_id AND x.lifecycle_state IN ('authorized','delivering','delivered','run_started','run_settled','cancellation_pending','cancelled')))")
    op.execute("UPDATE loop_rounds r SET decision_id = NULL FROM loop_decisions d WHERE r.decision_id = d.decision_id AND d.authority_committed_at IS NULL")
    _drop_unique("loop_decisions", ["round_id"])
    _index("loop_decisions", "uq_loop_round_committed_decision", ["round_id"], unique=True, where="authority_committed_at IS NOT NULL")
    _index("loop_rounds", "uq_loop_undecided_round", ["loop_id"], unique=True, where="decision_id IS NULL AND status IN ('observed','curated','waiting_workers')")
    _column("loop_patrol_sessions", sa.Column("observation_generation", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE loop_patrol_sessions s SET observation_id = r.observation_id FROM loop_rounds r WHERE s.round_id = r.round_id AND s.observation_id IS NULL")
    _drop_unique("loop_patrol_sessions", ["round_id"])
    _unique("loop_patrol_sessions", "uq_loop_patrol_session_observation", ["observation_id"])
    _column("loop_user_intents", sa.Column("introduced_observation_id", sa.String(32)))
    _column("loop_user_intents", sa.Column("introduced_generation", sa.Integer()))
    _column("loop_user_intents", sa.Column("snapshot_hash", sa.String(64)))
    _index("loop_user_intents", "ix_loop_user_intents_introduced_observation_id", ["introduced_observation_id"])
    _progress_work()
    _historical_permissions()


def _progress_work():
    table = "loop_progress_work"
    _column(table, sa.Column("work_id", sa.String(32)))
    _column(table, sa.Column("boundary_key", sa.String(64)))
    _column(table, sa.Column("round_ids", postgresql.JSONB(), nullable=False, server_default="[]"))
    _column(table, sa.Column("source_manifest", postgresql.JSONB(), nullable=False, server_default="{}"))
    _column(table, sa.Column("consolidation_payload", postgresql.JSONB()))
    _column(table, sa.Column("consolidation_hash", sa.String(64)))
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT w.observation_id, i.round_id, i.payload, o.envelope FROM loop_progress_work w JOIN loop_decision_inputs i USING (observation_id) JOIN loop_observations o USING (observation_id) WHERE w.consolidation_payload IS NULL")).mappings()
    for row in rows:
        observation_id = row["observation_id"]
        boundary = hashlib.sha256(json.dumps(["observation", observation_id], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        payload = {key: value for key, value in row["payload"].items() if key not in {"round_id", "observation_id", "observation_hash"}}
        payload.update(work_id=observation_id, origin_observation_id=observation_id,
            origin_observation_hash=row["payload"]["observation_hash"], origin_round_id=row["round_id"],
            round_ids=[row["round_id"]], mission=row["envelope"].get("mission") or {},
            budget=row["envelope"].get("budget") or {}, absorbed_sources={})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(sa.text("UPDATE loop_progress_work SET work_id = :identity, boundary_key = :boundary, round_ids = CAST(:rounds AS jsonb), source_manifest = CAST(:manifest AS jsonb), consolidation_payload = CAST(:payload AS jsonb), consolidation_hash = :hash WHERE observation_id = :identity"),
            {"identity": observation_id, "boundary": boundary, "rounds": json.dumps([row["round_id"]]), "manifest": json.dumps(row["payload"]["task_delta"]),
             "payload": encoded, "hash": hashlib.sha256(encoded.encode()).hexdigest()})
    primary = sa.inspect(connection).get_pk_constraint(table)
    if primary["constrained_columns"] != ["work_id"]:
        op.drop_constraint(primary["name"], table, type_="primary")
        op.create_primary_key("pk_loop_progress_work", table, ["work_id"])
    op.alter_column(table, "observation_id", existing_type=sa.String(32), nullable=True)
    op.alter_column(table, "work_id", existing_type=sa.String(32), nullable=False)
    op.alter_column(table, "boundary_key", existing_type=sa.String(64), nullable=False)
    _unique(table, "uq_progress_work_observation", ["observation_id"])
    _unique(table, "uq_progress_work_boundary", ["loop_id", "boundary_key"])
    _column("loop_task_progress", sa.Column("work_id", sa.String(32)))
    op.execute("UPDATE loop_task_progress p SET work_id = w.work_id FROM loop_progress_work w WHERE p.observation_id = w.observation_id AND p.work_id IS NULL")
    _unique("loop_task_progress", "uq_task_progress_work", ["work_id"])
    op.execute("UPDATE loop_progress_work w SET state = 'superseding', fence = fence + 1 FROM loop_rounds r, loop_decision_inputs i WHERE w.observation_id = i.observation_id AND i.round_id = r.round_id AND w.state NOT IN ('published','superseded') AND w.observation_id IS DISTINCT FROM r.observation_id AND NOT EXISTS (SELECT 1 FROM loop_decisions d WHERE d.observation_id = w.observation_id AND d.authority_committed_at IS NOT NULL)")


def _historical_permissions():
    connection = op.get_bind()
    decisions = connection.execute(sa.text("SELECT d.decision_id, d.loop_id, d.round_id, d.intent, l.equipment FROM loop_decisions d JOIN agent_loops l USING (loop_id) WHERE d.authority_committed_at IS NOT NULL AND NOT (d.intent ? 'context_permissions')")).mappings().all()
    modes = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
    for decision in decisions:
        permissions = {}
        run_bindings, observed_modes = [], {}
        directives = connection.execute(sa.text("SELECT x.directive_id, x.target_context_id, x.grant_revision, a.action_type, a.position, m.role, c.lane_policy FROM loop_directives x LEFT JOIN loop_actions a ON a.action_id = x.action_id LEFT JOIN loop_context_memberships m ON m.loop_id = x.loop_id AND m.context_id = x.target_context_id LEFT JOIN curation_lanes c ON c.lane_id = m.lane_id WHERE x.decision_id = :identity AND x.round_id = :round"),
            {"identity": decision["decision_id"], "round": decision["round_id"]}).mappings().all()
        for directive in directives:
            target = directive["target_context_id"]
            key = f"action:{directive['position']}" if directive["action_type"] in {"create_lane", "update_lane", "merge_contexts", "spawn_context", "recover_context"} else target
            runs = connection.execute(sa.text("SELECT run_id, task_id, equipment FROM desktop_runs WHERE directive_id = :directive AND loop_id = :loop AND round_id = :round"),
                {"directive": directive["directive_id"], "loop": decision["loop_id"], "round": decision["round_id"]}).mappings().all()
            source_modes = []
            for run in runs:
                equipment = dict(run["equipment"] or {})
                mode = equipment.get("access_mode")
                if mode not in modes or run["task_id"] != target:
                    continue
                source_modes.append(mode)
                observed_modes.setdefault(key, set()).add(mode)
                equipment.setdefault("round_authorization", {"loop_id": decision["loop_id"],
                    "round_id": decision["round_id"], "decision_id": decision["decision_id"],
                    "context_id": target, "grant_revision": directive["grant_revision"],
                    "access_mode": mode, "source": "migration"})
                run_bindings.append((run["run_id"], key, mode, equipment))
            if source_modes:
                mode = min(source_modes, key=modes.__getitem__)
            elif not runs:
                mode = (decision["equipment"] or {}).get("access_mode", "read-only")
                policy = (directive["lane_policy"] or {}).get("workspace_mode")
                if mode not in modes or policy == "read_only" or (directive["role"] != "primary" and policy != "isolated_write"):
                    mode = "read-only"
            else:
                continue
            permissions[key] = min((mode, permissions.get(key, mode)), key=modes.__getitem__)
        for run_id, key, mode, equipment in run_bindings:
            if mode != permissions[key] and equipment["round_authorization"].get("source") == "migration":
                equipment["round_authorization"]["migration_conflict"] = {
                    "reason": "inconsistent_context_access_modes", "selected_mode": permissions[key],
                    "observed_modes": sorted(observed_modes[key], key=modes.__getitem__)}
            connection.execute(sa.text("UPDATE desktop_runs SET equipment = CAST(:equipment AS jsonb) WHERE run_id = :run"),
                {"equipment": json.dumps(equipment), "run": run_id})
        intent = {**decision["intent"], "context_permissions": [{"context_id": key, "access_mode": mode} for key, mode in sorted(permissions.items())]}
        connection.execute(sa.text("UPDATE loop_decisions SET intent = CAST(:intent AS jsonb) WHERE decision_id = :identity"),
            {"intent": json.dumps(intent), "identity": decision["decision_id"]})


def downgrade():
    raise RuntimeError("多版本 Observation 与独立进度工作不可无损还原为单轮单观察；请按停止受理、排空和备份流程回退应用")
