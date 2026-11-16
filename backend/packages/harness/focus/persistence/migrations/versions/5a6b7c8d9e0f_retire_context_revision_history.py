"""Keep current Context state and explicitly owned in-flight input snapshots.

Historical archives are intentionally retired; restoring discarded history is not
an honest downgrade, so this migration is forward-only.
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "5a6b7c8d9e0f"
down_revision = "4a5b6c7d8e9f"
branch_labels = None
depends_on = None

_REF_FIELDS = ("context_id", "revision_id", "generation", "execution_thread_id",
               "checkpoint_ns", "checkpoint_id", "payload_mode")


def _json(value):
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()
                                if isinstance(item, datetime) else str(item)))


def _referenced_ids(value):
    if isinstance(value, dict):
        if "revision_id" in value and "context_id" in value:
            yield value["revision_id"]
        for item in value.values():
            yield from _referenced_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _referenced_ids(item)


def upgrade():
    connection = op.get_bind()
    # Reflect the *old* schema, independent of whichever application ORM is loaded.
    metadata = sa.MetaData()
    old = sa.Table("desktop_context_revisions", metadata, autoload_with=connection)
    refs = {row["revision_id"]: dict(row) for row in connection.execute(
        sa.select(*(old.c[name] for name in _REF_FIELDS))).mappings()}
    edges = defaultdict(list)
    for edge in connection.execute(sa.text(
        "SELECT target_revision_id, source_revision_id FROM desktop_context_revision_sources "
        "ORDER BY target_revision_id, position"
    )).mappings():
        edges[edge["target_revision_id"]].append(edge["source_revision_id"])

    def sources_for(revision_id):
        context_id = refs[revision_id]["context_id"]
        result, visited = [], set()
        pending = list(reversed(edges[revision_id]))
        while pending:
            source_id = pending.pop()
            if source_id in visited:
                continue
            visited.add(source_id)
            source = refs.get(source_id)
            if source is None:
                raise RuntimeError("Context migration encountered a missing source")
            if source["context_id"] == context_id:
                pending.extend(reversed(edges[source_id]))
            else:
                result.append({"source": source, "position": len(result)})
        return result

    published = set(connection.execute(sa.text("SELECT revision_id FROM desktop_context_publications")).scalars())
    owners = defaultdict(set)
    runs = list(connection.execute(sa.text(
        "SELECT run_id, context_revision_id FROM desktop_runs "
        "WHERE status IN ('pending','running','interrupted')"
    )).mappings())
    for row in runs:
        if row["context_revision_id"]:
            owners[("run", row["run_id"])].add(row["context_revision_id"])
    for row in connection.execute(sa.text(
        "SELECT run.run_id, r.revision_id FROM desktop_runs run "
        "JOIN desktop_context_revisions r ON r.context_id = run.task_id "
        "AND r.checkpoint_id = run.final_checkpoint_id AND r.origin_id = run.run_id "
        "WHERE run.status = 'interrupted'"
    )).mappings():
        owners[("run", row["run_id"])].add(row["revision_id"])
    for row in connection.execute(sa.text(
        "SELECT r.round_id, o.envelope FROM loop_rounds r "
        "JOIN loop_observations o ON o.observation_id = r.observation_id "
        "JOIN agent_loops l ON l.loop_id = r.loop_id "
        "WHERE r.status NOT IN ('settled','error','superseded') "
        "AND l.status IN ('running','pausing','paused','waiting_user','completing')"
    )).mappings():
        owners[("round", row["round_id"])].update(_referenced_ids(row["envelope"]))
    portfolios = list(connection.execute(sa.text(
        "SELECT portfolio_revision_id, source_frontier FROM curation_portfolio_revisions "
        "WHERE status NOT IN ('published','superseded','error','degraded')"
    )).mappings())
    for row in portfolios:
        owner = ("curation_portfolio", row["portfolio_revision_id"])
        owners[owner].update(_referenced_ids(row["source_frontier"]))
        candidates = connection.execute(sa.text(
            "SELECT base_context_revision_id, candidate_context_revision_id "
            "FROM curation_portfolio_lane_candidates WHERE portfolio_revision_id = :identity "
            "AND status IN ('pending','preparing','ready')"
        ), {"identity": row["portfolio_revision_id"]}).mappings()
        for candidate in candidates:
            owners[owner].update(value for value in candidate.values() if value)

    for row in connection.execute(sa.text(
        "SELECT p.revision_id AS owner_id, r.revision_id FROM patrol_context_revisions p "
        "JOIN patrol_context_bindings b ON b.binding_id = p.binding_id "
        "JOIN LATERAL (SELECT revision_id FROM desktop_context_revisions "
        "WHERE context_id = b.root_context_id AND checkpoint_id = p.source_checkpoint_id "
        "ORDER BY generation DESC LIMIT 1) r ON TRUE "
        "WHERE b.control_state <> 'stopped' AND "
        "(p.status IN ('observed','preparing','ready','publishing','approval_required') "
        "OR (p.status IN ('error','interrupted') AND p.source_checkpoint_id = b.desired_checkpoint_id))"
    )).mappings():
        owners[("context_curator", row["owner_id"])].add(row["revision_id"])

    for row in connection.execute(sa.text(
        "SELECT c.candidate_id, c.base_context_revision_id FROM loop_compression_candidates c "
        "JOIN agent_loops l ON l.loop_id = c.loop_id "
        "WHERE ((c.status = 'prepared' AND c.expires_at > CURRENT_TIMESTAMP) OR c.status = 'accepted') "
        "AND l.status IN ('running','pausing','paused','waiting_user','completing') "
        "AND NOT EXISTS (SELECT 1 FROM loop_compression_resolutions r "
        "WHERE r.candidate_id = c.candidate_id AND r.status IN ('applied','superseded','failed'))"
    )).mappings():
        owners[("compression_candidate", row["candidate_id"])].add(row["base_context_revision_id"])

    op.create_table("desktop_context_input_snapshots",
        sa.Column("snapshot_key", sa.String(64), primary_key=True),
        sa.Column("owner_kind", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(32), nullable=False),
        sa.Column("context_id", sa.String(32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_kind", "owner_id", "revision_id", name="uq_context_input_snapshot_owner"))
    for name in ("owner_kind", "owner_id", "revision_id", "context_id"):
        op.create_index("ix_desktop_context_input_snapshots_" + name,
                        "desktop_context_input_snapshots", [name])
    snapshots = sa.Table("desktop_context_input_snapshots", sa.MetaData(), autoload_with=connection)
    needed = {item for values in owners.values() for item in values}
    rows = {row["revision_id"]: dict(row) for row in connection.execute(
        sa.select(old).where(old.c.revision_id.in_(needed))).mappings()} if needed else {}
    payloads = {}
    for identity, row in rows.items():
        payload = {"ref": refs[identity], "sources": sources_for(identity)}
        payload.update({name: value for name, value in row.items() if name not in _REF_FIELDS})
        payloads[identity] = _json(payload)
    for (kind, owner_id), identities in owners.items():
        for identity in identities:
            if identity not in payloads:
                raise RuntimeError("Active Context input is missing during migration: " + str(identity))
            key = hashlib.sha256(json.dumps([kind, owner_id, identity], ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
            connection.execute(snapshots.insert().values(snapshot_key=key, owner_kind=kind,
                owner_id=owner_id, revision_id=identity, context_id=refs[identity]["context_id"],
                payload=payloads[identity], is_published=identity in published))

    # References remain immutable identity tokens. Current state can advance without
    # rewriting those tokens; exact reads use the owner's snapshot instead.
    inspector = sa.inspect(connection)
    for table in inspector.get_table_names():
        if table in {"desktop_context_revision_sources", "desktop_context_publications"}:
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "desktop_context_revisions" or (
                table in {"loop_semantic_index_artifacts", "loop_segment_projection_records"}
                and fk["referred_table"] == "desktop_threads"
                and fk["constrained_columns"] == ["context_id"]
            ):
                op.drop_constraint(fk["name"], table, type_="foreignkey")
    op.drop_table("desktop_context_revision_sources")
    op.drop_table("desktop_context_publications")
    connection.execute(sa.text(
        "DELETE FROM desktop_context_revisions r WHERE NOT EXISTS "
        "(SELECT 1 FROM desktop_threads t WHERE t.task_id = r.context_id "
        "AND t.current_revision_id = r.revision_id)"
    ))
    op.rename_table("desktop_context_revisions", "desktop_context_states")
    op.add_column("desktop_context_states", sa.Column("sources", JSONB(), nullable=False,
                                                       server_default=sa.text("'[]'::jsonb")))
    states = sa.Table("desktop_context_states", sa.MetaData(), autoload_with=connection)
    for identity in connection.execute(sa.select(states.c.revision_id)).scalars():
        connection.execute(states.update().where(states.c.revision_id == identity)
                           .values(sources=_json(sources_for(identity))))
    op.drop_constraint("uq_context_revision_generation", "desktop_context_states", type_="unique")
    op.create_unique_constraint("uq_context_state_context", "desktop_context_states", ["context_id"])
    for old_name, new_name in (
        ("uq_context_revision_execution_checkpoint", "uq_context_state_execution_checkpoint"),
        ("pk_desktop_context_revisions", "pk_desktop_context_states"),
        ("fk_context_revision_context", "fk_context_state_context"),
        *(("ck_context_revision_" + suffix, "ck_context_state_" + suffix)
          for suffix in ("generation_positive", "payload_mode", "projection_status", "origin_kind", "checkpoint_payload")),
    ):
        op.execute(sa.text('ALTER TABLE desktop_context_states RENAME CONSTRAINT "' + old_name + '" TO "' + new_name + '"'))
    op.execute(sa.text("ALTER INDEX ix_desktop_context_revisions_context_id RENAME TO ix_desktop_context_states_context_id"))
    # Retain the latest/current publication and live retries of the automatic
    # curator. Finished attempts must not become another full input archive.
    connection.execute(sa.text(
        "UPDATE patrol_context_revisions p SET status = 'superseded' "
        "FROM patrol_context_bindings b WHERE b.binding_id = p.binding_id "
        "AND b.control_state = 'stopped' AND p.status IN ('observed','preparing','ready') "
        "AND NOT EXISTS (SELECT 1 FROM patrol_context_attempts a "
        "WHERE a.revision_id = p.revision_id AND a.status IN ('pending','running'))"
    ))
    released = tuple(connection.execute(sa.text(
        "UPDATE patrol_context_revisions p SET source_payload = jsonb_build_object('input_released', true), "
        "authored_messages = '[]'::jsonb, execution_messages = '[]'::jsonb, "
        "disposition_manifest = '[]'::jsonb, repair_manifest = '[]'::jsonb, issues = '[]'::jsonb "
        "FROM patrol_context_bindings b WHERE b.binding_id = p.binding_id "
        "AND p.status IN ('published','unchanged','superseded','error','interrupted') "
        "AND p.revision_id <> (SELECT latest.revision_id FROM patrol_context_revisions latest "
        "WHERE latest.binding_id = p.binding_id ORDER BY latest.created_at DESC, latest.revision_id DESC LIMIT 1) "
        "AND p.source_checkpoint_id IS DISTINCT FROM b.published_checkpoint_id "
        "AND NOT (b.control_state <> 'stopped' AND p.status IN ('error','interrupted') "
        "AND p.source_checkpoint_id IS NOT DISTINCT FROM b.desired_checkpoint_id) "
        "AND NOT EXISTS (SELECT 1 FROM patrol_context_attempts a "
        "WHERE a.revision_id = p.revision_id AND a.status IN ('pending','running')) "
        "RETURNING p.revision_id"
    )).scalars())
    if released:
        attempts = sa.Table("patrol_context_attempts", sa.MetaData(), autoload_with=connection)
        connection.execute(attempts.update().where(attempts.c.revision_id.in_(released))
                           .values(raw_response={}, parsed_response={}))
        connection.execute(sa.text(
            "UPDATE desktop_runs SET input_messages = '[]'::jsonb "
            "WHERE origin = 'patrol_curation' AND status IN ('success','error','interrupted','timeout') "
            "AND run_id IN (SELECT run_id FROM patrol_context_attempts WHERE revision_id IN :released)"
        ).bindparams(sa.bindparam("released", expanding=True)), {"released": released})
    # Full artifacts follow the same live-reference boundary; local projection
    # proofs stay reusable by content and are deliberately not pruned here.
    connection.execute(sa.text(
        "DELETE FROM loop_semantic_index_artifacts i WHERE NOT EXISTS "
        "(SELECT 1 FROM desktop_context_states s WHERE s.revision_id = i.revision_id) "
        "AND NOT EXISTS (SELECT 1 FROM desktop_context_input_snapshots p "
        "WHERE p.revision_id = i.revision_id AND p.is_published) "
        "AND NOT EXISTS (SELECT 1 FROM loop_planning_retrieval_sessions p "
        "JOIN loop_rounds r ON r.round_id = p.round_id "
        "WHERE r.status NOT IN ('settled','error','superseded') "
        "AND COALESCE(p.payload -> 'authorized_index_ids', '[]'::jsonb) ? i.index_id)"
    ))


def downgrade():
    raise RuntimeError("Context revision history was retired; discarded history cannot be reconstructed by downgrade")
