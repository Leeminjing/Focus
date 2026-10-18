"""Notify desktop state readers after committed changes, including external writers."""

from alembic import op

revision = "e36d7e8f9012"
down_revision = "e25c6d7e8f90"
branch_labels = None
depends_on = None


# UPDATE predicates exclude lease renewals, token output and unrelated UI state.
_TARGETS = (
    ("desktop_threads", "focus_tasks_changed", "title, workspace_id, archived_at, deleted_at"),
    ("desktop_runs", "focus_tasks_changed", "status"),
    ("agent_loops", "focus_tasks_changed", "status, initial_context_id, interaction_mode"),
    ("loop_pending_decisions", "focus_tasks_changed", "status, kind"),
    ("loop_wait_requests", "focus_tasks_changed", "status, kind"),
    ("desktop_materials", "focus_materials_changed", "retention, relative_path, task_id, workspace_id"),
    ("desktop_workspaces", "focus_tasks_changed", "path, display_name"),
    ("desktop_workspaces", "focus_materials_changed", "path"),
)


def upgrade():
    op.execute("""CREATE FUNCTION focus_desktop_invalidate() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify(TG_ARGV[0], '');
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql""")
    for table, channel, fields in _TARGETS:
        name = f"{table}_{channel}"
        op.execute(f"""CREATE TRIGGER {name}_rows AFTER INSERT OR DELETE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION focus_desktop_invalidate('{channel}')""")
        changed = " OR ".join(f"OLD.{field.strip()} IS DISTINCT FROM NEW.{field.strip()}"
                              for field in fields.split(","))
        op.execute(f"""CREATE TRIGGER {name}_update AFTER UPDATE OF {fields} ON {table}
            FOR EACH ROW WHEN ({changed})
            EXECUTE FUNCTION focus_desktop_invalidate('{channel}')""")
    op.execute("""CREATE TRIGGER desktop_runs_attention_changed AFTER UPDATE OF workspace_result
        ON desktop_runs FOR EACH ROW
        WHEN ((OLD.workspace_result -> 'task_attention') IS DISTINCT FROM
              (NEW.workspace_result -> 'task_attention'))
        EXECUTE FUNCTION focus_desktop_invalidate('focus_tasks_changed')""")


def downgrade():
    op.execute("DROP TRIGGER desktop_runs_attention_changed ON desktop_runs")
    for table, channel, _fields in reversed(_TARGETS):
        name = f"{table}_{channel}"
        op.execute(f"DROP TRIGGER {name}_update ON {table}")
        op.execute(f"DROP TRIGGER {name}_rows ON {table}")
    op.execute("DROP FUNCTION focus_desktop_invalidate()")
