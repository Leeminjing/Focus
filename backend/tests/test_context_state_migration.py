"""Fresh and populated-old-head upgrades retire archives without losing live inputs."""

import asyncio
import hashlib
import importlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

MIGRATIONS = Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"


def _engine():
    return sa.create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))


def test_fresh_head_has_current_state_and_owned_snapshots(isolated_postgres_database):
    engine = _engine()
    try:
        inspector = sa.inspect(engine)
        names = set(inspector.get_table_names())
        assert {"desktop_context_states", "desktop_context_input_snapshots"} <= names
        assert not {"desktop_context_revisions", "desktop_context_revision_sources", "desktop_context_publications"} & names
        assert {"curation_programs", "curation_source_subscriptions", "curation_lanes",
                "curation_portfolio_revisions", "curation_portfolio_lane_candidates",
                "curation_attempts", "curation_portfolio_publication_attempts",
                "curation_outbox_events", "curation_outbox_deliveries",
                "patrol_drafts", "patrol_deployment_definitions"} <= names
        assert any(item["column_names"] == ["context_id"]
                   for item in inspector.get_unique_constraints("desktop_context_states"))
        assert not inspector.get_foreign_keys("desktop_context_input_snapshots")
        assert not inspector.get_foreign_keys("loop_semantic_index_artifacts")
        assert not inspector.get_foreign_keys("loop_segment_projection_records")
        for table in names:
            assert not any(item["referred_table"] == "desktop_context_revisions"
                           for item in inspector.get_foreign_keys(table))
    finally:
        engine.dispose()


def test_populated_old_head_preserves_latest_and_only_live_inputs(isolated_postgres_database, monkeypatch):
    original = make_url(os.environ["FOCUS_DATABASE_URL"])
    name = "focus_state_upgrade_" + uuid.uuid4().hex
    admin = original.set(drivername="postgresql", database="postgres", query={}).render_as_string(hide_password=False)
    target = original.set(database=name, query={})
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    monkeypatch.setenv("FOCUS_DATABASE_URL", target.render_as_string(hide_password=False))
    config = Config(str(MIGRATIONS))
    engine = _engine()
    try:
        command.upgrade(config, "4a5b6c7d8e9f")
        metadata = sa.MetaData()
        tables = {name: sa.Table(name, metadata, autoload_with=engine) for name in (
            "desktop_workspaces", "desktop_threads", "desktop_context_revisions",
            "desktop_context_revision_sources", "desktop_context_publications", "desktop_runs",
            "curation_programs", "curation_lanes", "curation_portfolio_revisions",
            "curation_portfolio_lane_candidates", "agent_loops", "loop_rounds", "loop_observations",
            "patrol_deployment_definitions", "loop_semantic_index_artifacts", "loop_planning_retrieval_sessions",
            "loop_pending_decisions", "loop_compression_candidates", "loop_segment_projection_records",
            "patrol_agents", "patrol_context_bindings", "patrol_context_revisions", "patrol_context_attempts")}
        with engine.begin() as connection:
            connection.execute(tables["desktop_workspaces"].insert().values(workspace_id="ws", path="/upgrade", display_name="Upgrade"))
            connection.execute(tables["desktop_threads"].insert(), [
                {"task_id": identity, "workspace_id": "ws", "thread_id": "thread-" + identity,
                 "title": identity, "ui_state": {}} for identity in ("source", "context")])
            for identity, context_id, generation, origin in (
                ("source-v1", "source", 1, "root"), ("context-v1", "context", 1, "manual_derive"),
                ("context-v2", "context", 2, "curation"), ("context-v3", "context", 3, "run_settled"),
                ("obsolete-v4", "context", 4, "definition_update"), ("candidate-v5", "context", 5, "curation"), ("resume-v6", "context", 6, "run_settled")):
                connection.execute(tables["desktop_context_revisions"].insert().values(
                    revision_id=identity, context_id=context_id, generation=generation,
                    execution_thread_id="execution-" + identity, checkpoint_ns="", checkpoint_id="checkpoint-" + identity,
                    payload_mode="checkpoint", authored_messages=[], execution_messages=[], repair_manifest=[],
                    issues=[], initial_message_ids=[], content_hash=hashlib.sha256(identity.encode()).hexdigest(),
                    projection_status="valid", origin_kind=origin, origin_id="live-run" if identity == "resume-v6" else None))
                if identity != "candidate-v5":
                    connection.execute(tables["desktop_context_publications"].insert().values(revision_id=identity, context_id=context_id))
            connection.execute(tables["desktop_context_revision_sources"].insert(), [
                {"source_edge_id": "edge-cross", "target_revision_id": "context-v2", "source_context_id": "source",
                 "source_revision_id": "source-v1", "source_checkpoint_id": "checkpoint-source-v1", "position": 0},
                {"source_edge_id": "edge-self", "target_revision_id": "context-v3", "source_context_id": "context",
                 "source_revision_id": "context-v2", "source_checkpoint_id": "checkpoint-context-v2", "position": 0}])
            for context_id, identity in (("source", "source-v1"), ("context", "context-v3")):
                connection.execute(tables["desktop_threads"].update().where(tables["desktop_threads"].c.task_id == context_id).values(current_revision_id=identity))
            for identity, status, revision_id in (("live-run", "interrupted", "context-v1"), ("old-run", "success", "obsolete-v4")):
                connection.execute(tables["desktop_runs"].insert().values(run_id=identity, task_id="context", agent_id="main",
                    kind="main", status=status, origin="patrol_curation" if identity == "old-run" else "direct_user",
                    input_messages=[{"body": "old curator input"}] if identity == "old-run" else [], context_revision_id=revision_id,
                    final_checkpoint_id="checkpoint-resume-v6" if identity == "live-run" else None))
            connection.execute(tables["curation_programs"].insert().values(program_id="program", workspace_id="ws"))
            connection.execute(tables["curation_lanes"].insert().values(lane_id="lane", program_id="program", purpose="Keep", normalized_purpose="keep"))
            connection.execute(tables["curation_portfolio_revisions"].insert().values(
                portfolio_revision_id="portfolio", program_id="program", generation=1, source_frontier=[{
                    "context_id": "context", "revision_id": "context-v2"}], frontier_hash="a" * 64,
                base_program_revision=0, status="ready"))
            connection.execute(tables["curation_portfolio_lane_candidates"].insert().values(
                candidate_id="candidate", portfolio_revision_id="portfolio", lane_id="lane", action="update",
                target_context_id="context", base_publisher_epoch=1, base_context_revision_id="context-v2",
                candidate_context_revision_id="candidate-v5", purpose="Keep", status="ready"))
            connection.execute(tables["agent_loops"].insert().values(loop_id="loop", workspace_id="ws",
                initial_context_id="context", holder_id="migration", status="running"))
            connection.execute(tables["loop_rounds"].insert().values(round_id="round", loop_id="loop", number=1,
                status="observed", observation_id="observation", authority_revision=1, goal_revision=1,
                frontier_hash="a" * 64, workspace_revision=1))
            connection.execute(tables["loop_observations"].insert().values(observation_id="observation", loop_id="loop",
                round_id="round", generation=1, envelope={"portfolio_frontier": [{"context_id": "context", "revision_id": "context-v2"}]},
                envelope_hash="b" * 64))
            connection.execute(tables["patrol_deployment_definitions"].insert().values(definition_id="definition",
                run_id="old-run", document_hash="c" * 64, document={"instructions": "keep edited input"},
                compiled_plan={"messages": []}, sources={}, execution_mode="restart"))
            for identity, revision_id in (("current-index", "context-v3"), ("pinned-index", "context-v2"),
                                          ("stale-index", "obsolete-v4"), ("planning-index", "obsolete-v4")):
                connection.execute(tables["loop_semantic_index_artifacts"].insert().values(index_id=identity,
                    context_id="context", revision_id=revision_id, source_content_hash="d" * 64,
                    index_schema_version="legacy", segmenter_version="legacy", projector_version=identity,
                    status="ready", payload={}))
            connection.execute(tables["loop_planning_retrieval_sessions"].insert().values(session_id="planning",
                loop_id="loop", round_id="round", frontier_hash="a" * 64, catalog_id="catalog", state="queried",
                budget={}, usage={}, payload={"authorized_index_ids": ["planning-index"]}))
            connection.execute(tables["loop_pending_decisions"].insert().values(pending_decision_id="pending",
                loop_id="loop", kind="compression", payload={}))
            connection.execute(tables["loop_compression_candidates"].insert().values(candidate_id="compression",
                loop_id="loop", pending_decision_id="pending", round_id="round", context_id="context",
                base_context_revision_id="context-v1", base_checkpoint_id="checkpoint-context-v1", frontier_hash="a" * 64,
                authority_revision=1, goal_revision=1, policy_revision=1, normalized_ranges=[], replacement_hash="b" * 64,
                candidate_hash="c" * 64, before_tokens=100, after_tokens=50, status="prepared",
                expires_at=datetime.now(UTC) + timedelta(hours=1)))
            connection.execute(tables["loop_segment_projection_records"].insert().values(record_id="proof",
                context_id="context", cache_key="proof", payload={"proof": "preserved"}))
            connection.execute(tables["patrol_agents"].insert().values(agent_id="curator", task_id="context",
                checkpoint_ns="curator", system_prompt="Curate", frozen_messages=[], equipment={}, mode="context_curator"))
            connection.execute(tables["patrol_context_bindings"].insert().values(binding_id="binding", agent_id="curator",
                root_context_id="context", control_state="following", health_state="idle",
                desired_checkpoint_id="checkpoint-context-v2", published_checkpoint_id="checkpoint-context-v3"))
            for offset, (identity, source, status) in enumerate((("finished", "context-v1", "published"),
                ("retry", "context-v2", "error"), ("published", "context-v3", "published"), ("newest", "resume-v6", "observed"))):
                connection.execute(tables["patrol_context_revisions"].insert().values(revision_id=identity, binding_id="binding",
                    source_checkpoint_id="checkpoint-" + source, base_binding_revision=0, status=status,
                    source_payload={"body": identity}, authored_messages=[{"body": identity}],
                    created_at=datetime.now(UTC) + timedelta(seconds=offset)))
            connection.execute(tables["patrol_context_attempts"].insert().values(attempt_id="finished-attempt",
                revision_id="finished", attempt_number=1, run_id="old-run", model_name="fake", output_method="json_schema",
                status="success", raw_response={"body": "discarded response"}, parsed_response={"body": "discarded response"}))
            connection.execute(tables["patrol_agents"].insert().values(agent_id="stopped-curator", task_id="context",
                checkpoint_ns="stopped-curator", system_prompt="Curate", frozen_messages=[], equipment={}, mode="context_curator"))
            connection.execute(tables["patrol_context_bindings"].insert().values(binding_id="stopped-binding", agent_id="stopped-curator",
                root_context_id="context", control_state="stopped", health_state="idle"))
            for offset, (identity, source, status) in enumerate((("stopped-old", "context-v1", "error"),
                ("stopped-observed", "context-v2", "observed"), ("stopped-newest", "context-v3", "error"))):
                connection.execute(tables["patrol_context_revisions"].insert().values(revision_id=identity, binding_id="stopped-binding",
                    source_checkpoint_id="checkpoint-" + source, base_binding_revision=0, status=status,
                    source_payload={"body": identity}, created_at=datetime.now(UTC) + timedelta(seconds=offset)))
            connection.execute(tables["loop_observations"].update().where(tables["loop_observations"].c.observation_id == "observation")
                .values(envelope={"portfolio_frontier": [{"context_id": "context", "revision_id": "missing"}]}))
        with pytest.raises(RuntimeError, match="Active Context input is missing"):
            command.upgrade(config, "head")
        assert "desktop_context_revisions" in sa.inspect(engine).get_table_names()
        assert "desktop_context_input_snapshots" not in sa.inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(tables["loop_observations"].update().where(tables["loop_observations"].c.observation_id == "observation")
                .values(envelope={"portfolio_frontier": [{"context_id": "context", "revision_id": "context-v2"}]}))
        command.upgrade(config, "head")
        assert not sa.inspect(engine).get_foreign_keys("loop_semantic_index_artifacts")
        assert not sa.inspect(engine).get_foreign_keys("loop_segment_projection_records")
        with engine.connect() as connection:
            states = connection.execute(sa.text("SELECT * FROM desktop_context_states ORDER BY context_id")).mappings().all()
            assert [(row["context_id"], row["revision_id"]) for row in states] == [("context", "context-v3"), ("source", "source-v1")]
            assert states[0]["generation"] == 3  # Pointer wins over an unpublished higher generation.
            assert states[0]["sources"][0]["source"]["revision_id"] == "source-v1"
            snapshots = connection.execute(sa.text("SELECT * FROM desktop_context_input_snapshots")).mappings().all()
            actual = {(row["owner_kind"], row["owner_id"], row["revision_id"]) for row in snapshots}
            assert actual == {("run", "live-run", "context-v1"), ("curation_portfolio", "portfolio", "context-v2"),
                              ("curation_portfolio", "portfolio", "candidate-v5"), ("round", "round", "context-v2"),
                              ("run", "live-run", "resume-v6"), ("compression_candidate", "compression", "context-v1"),
                              ("context_curator", "retry", "context-v2"), ("context_curator", "newest", "resume-v6")}
            from backend.app.desktop.contexts.evolution.schemas import (
                ContextRevisionContract,
            )
            for row in snapshots:
                payload = ContextRevisionContract.model_validate(row["payload"])
                assert payload.ref.revision_id == row["revision_id"]
                assert row["is_published"] is (row["revision_id"] != "candidate-v5")
                key = hashlib.sha256(json.dumps([row["owner_kind"], row["owner_id"], row["revision_id"]], ensure_ascii=False,
                                                separators=(",", ":")).encode()).hexdigest()
                assert row["snapshot_key"] == key
            assert set(connection.execute(sa.text("SELECT index_id FROM loop_semantic_index_artifacts")).scalars()) == {"current-index", "pinned-index", "planning-index"}
            assert connection.scalar(sa.text("SELECT payload FROM loop_segment_projection_records WHERE record_id='proof'")) == {"proof": "preserved"}
            assert connection.scalar(sa.text("SELECT count(*) FROM desktop_threads")) == 2
            assert connection.scalar(sa.text("SELECT count(*) FROM curation_portfolio_lane_candidates")) == 1
            assert connection.scalar(sa.text("SELECT document FROM patrol_deployment_definitions WHERE definition_id='definition'")) == {"instructions": "keep edited input"}
            assert connection.scalar(sa.text("SELECT context_revision_id FROM desktop_runs WHERE run_id='old-run'")) == "obsolete-v4"
            assert connection.scalar(sa.text("SELECT source_payload FROM patrol_context_revisions WHERE revision_id='finished'")) == {"input_released": True}
            for identity in ("retry", "published", "newest"):
                assert connection.scalar(sa.text("SELECT source_payload FROM patrol_context_revisions WHERE revision_id=:identity"), {"identity": identity}) == {"body": identity}
            assert connection.scalar(sa.text("SELECT raw_response FROM patrol_context_attempts WHERE attempt_id='finished-attempt'")) == {}
            assert connection.scalar(sa.text("SELECT parsed_response FROM patrol_context_attempts WHERE attempt_id='finished-attempt'")) == {}
            assert connection.scalar(sa.text("SELECT input_messages FROM desktop_runs WHERE run_id='old-run'")) == []
            assert connection.scalar(sa.text("SELECT source_payload FROM patrol_context_revisions WHERE revision_id='stopped-old'")) == {"input_released": True}
            assert connection.scalar(sa.text("SELECT source_payload FROM patrol_context_revisions WHERE revision_id='stopped-newest'")) == {"body": "stopped-newest"}
            assert connection.scalar(sa.text("SELECT status FROM patrol_context_revisions WHERE revision_id='stopped-observed'")) == "superseded"
            assert connection.scalar(sa.text("SELECT source_payload FROM patrol_context_revisions WHERE revision_id='stopped-observed'")) == {"input_released": True}
        async def release_portfolio():
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from backend.app.desktop.contexts.evolution.repository import (
                ContextRevisionRepository,
            )
            async_engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            try:
                async with async_sessionmaker(async_engine).begin() as session:
                    await ContextRevisionRepository().release(session, "curation_portfolio", "portfolio")
            finally:
                await async_engine.dispose()
        asyncio.run(release_portfolio())
        with engine.connect() as connection:
            assert not connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM desktop_context_input_snapshots WHERE owner_kind='curation_portfolio' AND owner_id='portfolio')"))
            assert not connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM desktop_context_input_snapshots WHERE revision_id='candidate-v5')"))
        with pytest.raises(RuntimeError, match="discarded history"):
            importlib.import_module("focus.persistence.migrations.versions.5a6b7c8d9e0f_retire_context_revision_history").downgrade()
    finally:
        engine.dispose()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
