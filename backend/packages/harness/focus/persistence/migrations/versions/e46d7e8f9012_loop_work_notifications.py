"""Wake Loop consumers only after committed scheduling facts change.

Notifications are hints containing identities and consumer names. Durable rows,
leases, receipts and fencing remain authoritative; no notification is a job.
"""

from alembic import op

revision = "e46d7e8f9012"
down_revision = "e36d7e8f9012"
branch_labels = None
depends_on = None


# Do not notify on lease renewal, accounting, queue reasons or identical writes.
_TARGETS = (
    ("agent_loops", "status", "loop_registry,rounds,workers,context_runs,portfolio_publications", "TRUE"),
    ("loop_user_intents", None, "rounds,cognition_cancellations", "TRUE"),
    ("loop_rounds", "status,observation_id,decision_id", "rounds,context_runs", "TRUE"),
    ("loop_worker_requests", "status,completed_at", "workers,rounds,maintenance", "NEW.status <> 'running'"),
    ("loop_decisions", "status,authority_committed_at", "portfolio_publications,context_runs,rounds", "TRUE"),
    ("loop_directives", "status,lifecycle_state", "context_runs", "NEW.status = 'created' AND NEW.lifecycle_state = 'authorized'"),
    ("loop_journal_events", None, "fact_projector", "TRUE"),
    ("loop_event_outbox", "status", "cognition_cancellations,rounds", "NEW.event_type IN ('loop.cognition.preempted','loop.execution.cancel_requested')"),
    ("run_outbox_events", None, "run_events", "TRUE"),
    # These consumers take one item per tick; completion also drains any backlog.
    ("loop_compression_resolutions", "status", "compression_resolutions", "TRUE"),
    ("loop_progress_work", "state", "task_progress", "TRUE"),
    ("loop_progress_heads", "progress_id", "rounds", "TRUE"),
)


def upgrade():
    op.execute("""CREATE FUNCTION focus_loop_work_notify() RETURNS trigger AS $$
        DECLARE identity text;
        BEGIN
            IF TG_TABLE_NAME = 'run_outbox_events' THEN
                SELECT loop_id INTO identity FROM desktop_runs WHERE run_id = NEW.run_id;
            ELSE
                identity := to_jsonb(NEW)->>'loop_id';
            END IF;
            IF identity IS NOT NULL THEN
                PERFORM pg_notify('focus_loop_work', json_build_object(
                    'loop_id', identity, 'components', string_to_array(TG_ARGV[0], ','))::text);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE FUNCTION focus_loop_capacity_notify() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('focus_loop_work', json_build_object(
                'loop_id', NULL, 'components', string_to_array(TG_ARGV[0], ','))::text);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql""")
    for table, fields, components, predicate in _TARGETS:
        op.execute(f"""CREATE TRIGGER {table}_work_insert AFTER INSERT ON {table}
            FOR EACH ROW WHEN ({predicate})
            EXECUTE FUNCTION focus_loop_work_notify('{components}')""")
        if fields:
            changed = " OR ".join(f"OLD.{field} IS DISTINCT FROM NEW.{field}" for field in fields.split(","))
            op.execute(f"""CREATE TRIGGER {table}_work_update AFTER UPDATE OF {fields} ON {table}
                FOR EACH ROW WHEN (({changed}) AND ({predicate}))
                EXECUTE FUNCTION focus_loop_work_notify('{components}')""")
    # Pools share capacity across Loops: a release can unblock a different Loop.
    op.execute("""CREATE TRIGGER loop_worker_capacity AFTER UPDATE OF completed_at ON loop_worker_requests
        FOR EACH ROW WHEN (NEW.completed_at IS NOT NULL AND OLD.completed_at IS DISTINCT FROM NEW.completed_at)
        EXECUTE FUNCTION focus_loop_capacity_notify('workers')""")
    op.execute("""CREATE TRIGGER loop_run_capacity AFTER UPDATE OF settled_at ON desktop_runs
        FOR EACH ROW WHEN (NEW.loop_id IS NOT NULL AND NEW.settled_at IS NOT NULL
            AND OLD.settled_at IS DISTINCT FROM NEW.settled_at)
        EXECUTE FUNCTION focus_loop_capacity_notify('context_runs')""")


def downgrade():
    op.execute("DROP TRIGGER loop_run_capacity ON desktop_runs")
    op.execute("DROP TRIGGER loop_worker_capacity ON loop_worker_requests")
    for table, fields, _components, _predicate in reversed(_TARGETS):
        if fields:
            op.execute(f"DROP TRIGGER {table}_work_update ON {table}")
        op.execute(f"DROP TRIGGER {table}_work_insert ON {table}")
    op.execute("DROP FUNCTION focus_loop_capacity_notify()")
    op.execute("DROP FUNCTION focus_loop_work_notify()")
