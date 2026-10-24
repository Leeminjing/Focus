"""Read-only discovery/export of frozen semantic-index benchmark inputs.

Run ``python -m backend.scripts.semantic_index_corpus --help`` from the repo.
The source connection never runs migrations or checkpointer setup. Exports
contain local conversation text; keep them outside tracked report directories.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Literal

import psycopg
from focus.config.app_config import get_app_config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.app.desktop.contexts.evolution import ContextRevisionReader, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.contexts.evolution.models import ContextPublicationReceipt, ContextRevision
from backend.app.desktop.contexts.evolution.repository import ContextRevisionRepositoryError


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: ContextRevisionRef
    source_content_hash: str
    messages: tuple[dict, ...]
    semantic_hash: str

    @model_validator(mode="after")
    def check_content(self):
        require_jsonb_text(self.messages)
        if self.semantic_hash != stable_expansion_hash("benchmark-semantic-snapshot-v1", self.messages):
            raise ValueError("benchmark semantic content hash mismatch")
        ProtocolSafeRevisionSegmenter().segment(RevisionSemanticIndexer._messages(self.messages))
        return self


class CorpusCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    category: Literal["short", "long_tool", "multiple_append", "selected"]
    snapshots: tuple[Snapshot, ...]

    @model_validator(mode="after")
    def check_append(self):
        if not self.snapshots or not self.snapshots[0].messages:
            raise ValueError("benchmark case needs nonempty frozen input")
        for old, new in zip(self.snapshots, self.snapshots[1:]):
            if (old.source.context_id != new.source.context_id
                    or old.source.generation >= new.source.generation
                    or len(new.messages) <= len(old.messages)
                    or new.messages[:len(old.messages)] != old.messages):
                raise ValueError("benchmark snapshots must be strictly append-only within one Context")
        return self


class BenchmarkCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["semantic-index-benchmark-corpus-v1"] = "semantic-index-benchmark-corpus-v1"
    cases: tuple[CorpusCase, ...]

    @model_validator(mode="after")
    def check_cases(self):
        if not self.cases or len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("benchmark needs uniquely named cases")
        return self


def load_corpus(path):
    return BenchmarkCorpus.model_validate_json(Path(path).read_text(encoding="utf-8"))


def require_jsonb_text(value):
    """Reject binary/surrogate text that cannot enter the index's JSONB store."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("postgres_jsonb_incompatible_text")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("postgres_jsonb_incompatible_text") from exc
    elif isinstance(value, dict):
        for key, item in value.items():
            require_jsonb_text(key)
            require_jsonb_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_jsonb_text(item)


def summarize_reports(directory):
    """Keep the two independent variables visible; reject mixed control settings."""
    controls, rows, inputs = {}, [], {}
    for path in sorted(Path(directory).glob("*-segment-*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        key = (report["corpus_case_hash"], report["repeat"])
        control = {name: report.get(name) for name in (
            "configured_model", "provider_model", "output_token_limit", "model_identities",
            "concurrency", "budget_limits",
        )}
        control["role_output_token_limits"] = report.get("role_output_token_limits") or {}
        if key in controls and controls[key] != control:
            raise ValueError("incomparable benchmark reports: model, role output limits, concurrency or budget differs")
        controls[key] = control
        inputs[report["corpus_case_hash"]] = report["frozen_sources"]
        for phase_name, phase in report["phases"].items():
            quality = phase.get("quality", {})
            rows.append({
                "case": report["case"], "corpus_case_hash": key[0], "repeat": key[1],
                "max_messages": report["segment_size"], "max_estimated_tokens": report["segment_token_limit"],
                "local_prewarm": report.get("local_prewarm", "off"),
                "phase": phase_name, "evaluation_complete": report.get("evaluation_complete", False),
                **{name: phase.get(name) for name in (
                    "duration_ms", "model_calls", "billed_model_calls", "input_tokens", "output_tokens",
                    "retries", "retry_rate", "blocker_code", "blocker_summary", "status", "reason",
                    "segments", "reused_segments", "recomputed_segments",
                )},
                **{name: quality.get(name) for name in (
                    "state", "fallback_ratio", "citation_validity", "accepted_citation_message_coverage",
                    "rejection_codes",
                )},
            })
    if not rows:
        raise ValueError("no benchmark reports found")
    return {
        "version": "semantic-index-benchmark-summary-v1",
        "comparison_rule": "vary one of max_messages, max_estimated_tokens or local_prewarm at a time; compare matching cases/repeats/phases and include prewarm cost",
        "semantic_review": "message citation coverage is a proxy; semantic completeness has no human gold set",
        "controls": [{"corpus_case_hash": key[0], "repeat": key[1], **value} for key, value in controls.items()],
        "source_inputs": inputs, "rows": rows,
    }


def snapshot_stats(snapshot):
    messages = RevisionSemanticIndexer._messages(snapshot.messages)
    return {
        "context_id": snapshot.source.context_id, "revision_id": snapshot.source.revision_id,
        "generation": snapshot.source.generation, "messages": len(messages),
        "estimated_tokens": ProtocolSafeRevisionSegmenter.estimated_tokens(messages),
        "max_tool_chars": max((len(json.dumps(m.content, ensure_ascii=False))
                               for m in messages if m.role == "tool"), default=0),
    }


def select_cases(snapshots, *, automatic):
    groups = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.messages:
            groups[snapshot.source.context_id].append(snapshot)
    chains = []
    for context_id in sorted(groups):
        chain = []
        for snapshot in sorted(groups[context_id], key=lambda s: s.source.generation):
            if chain and (len(snapshot.messages) <= len(chain[-1].messages)
                          or snapshot.messages[:len(chain[-1].messages)] != chain[-1].messages):
                chains.append(chain)
                chain = []
            chain.append(snapshot)
        if chain:
            chains.append(chain)
    if not automatic:
        return tuple(CorpusCase(name=f"selected-{i}", category="selected", snapshots=chain)
                     for i, chain in enumerate(chains))
    if not chains:
        raise ValueError("no readable nonempty semantic revisions")
    candidates = [s for chain in chains for s in chain]
    shorts = [s for s in candidates if len(s.messages) <= 12
              and snapshot_stats(s)["estimated_tokens"] <= 2048
              and snapshot_stats(s)["max_tool_chars"] < 4096]
    if not shorts:
        raise ValueError("need a short dialogue (<=12 messages, <=2048 estimated tokens, no long tool output)")
    short = min([s for s in shorts if len(s.messages) >= 2] or shorts,
                key=lambda s: (len(s.messages), s.source.revision_id))
    tools = [s for s in candidates if snapshot_stats(s)["max_tool_chars"] >= 4096]
    appends = [chain for chain in chains if len(chain) >= 3]
    if not tools or not appends:
        raise ValueError("need a long tool output (>=4096 chars) and three append-only revisions; use explicit IDs for partial coverage")
    tool = min([s for s in tools if len(s.messages) > 30] or tools,
               key=lambda s: snapshot_stats(s)["estimated_tokens"])
    # Bound sample count, not message content; never trim a frozen Revision.
    append_chain = min([chain for chain in appends if len(chain[-1].messages) > 30] or appends,
                       key=lambda chain: sum(len(s.messages) for s in chain[-3:]))[-3:]
    return (
        CorpusCase(name="short", category="short", snapshots=(short,)),
        CorpusCase(name="long-tool", category="long_tool", snapshots=(tool,)),
        CorpusCase(name="multiple-append", category="multiple_append", snapshots=append_chain),
    )


async def read_snapshots(config_path, *, context_ids=(), revision_ids=(), limit=200):
    config = get_app_config(config_path)
    url = make_url(config.database.url)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"),
                                 connect_args={"server_settings": {"default_transaction_read_only": "on"}, "timeout": 5})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshots, skipped = [], []
    try:
        # A separate read-only psycopg connection serves exact historical checkpoints.
        conn = await psycopg.AsyncConnection.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False),
            autocommit=True, prepare_threshold=0, row_factory=dict_row,
            options="-c default_transaction_read_only=on", connect_timeout=5,
        )
        async with conn:
            repository = ContextRevisionRepository()
            reader = ContextRevisionReader(repository, AsyncPostgresSaver(conn))
            async with sessions() as session:
                query = select(ContextRevision).join(ContextPublicationReceipt).order_by(
                    ContextRevision.created_at.desc(), ContextRevision.revision_id
                )
                if context_ids:
                    query = query.where(ContextRevision.context_id.in_(context_ids))
                if revision_ids:
                    query = query.where(ContextRevision.revision_id.in_(revision_ids))
                rows = (await session.scalars(query.limit(limit))).all()
                for row in rows:
                    try:
                        revision = await repository.get_by_id(session, row.revision_id)
                        view = await reader.read(session, revision.ref, "semantic")
                        messages = tuple(view.messages)
                        require_jsonb_text(messages)
                        snapshots.append(Snapshot(
                            source=revision.ref, source_content_hash=revision.content_hash,
                            messages=messages,
                            semantic_hash=stable_expansion_hash("benchmark-semantic-snapshot-v1", messages),
                        ))
                    except (ValueError, KeyError, TypeError, LookupError, ContextRevisionRepositoryError) as exc:
                        skipped.append({"revision_id": row.revision_id,
                                        "error": "postgres_jsonb_incompatible_text" if "postgres_jsonb_incompatible_text" in str(exc)
                                        else type(exc).__name__})
    finally:
        await engine.dispose()
    missing = set(revision_ids) - {s.source.revision_id for s in snapshots}
    if missing:
        raise ValueError(f"requested revisions are unavailable or invalid: {sorted(missing)}")
    if set(context_ids) - {s.source.context_id for s in snapshots}:
        raise ValueError("requested Context is unavailable or invalid")
    return snapshots, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "export", "summarize"))
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--context-id", action="append", default=[])
    parser.add_argument("--revision-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--auto", action="store_true", help="select short, long-tool and multiple-append cases")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reports", type=Path, help="report directory for summarize (no database access)")
    args = parser.parse_args()
    if args.limit < 1 or (args.action == "export" and args.output is None):
        parser.error("positive --limit and export --output are required")
    if args.action == "summarize":
        if args.reports is None or args.output is None:
            parser.error("summarize requires --reports and --output")
        summary = summarize_reports(args.reports)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(summary['rows'])} measured phase rows to {args.output.resolve()}")
        return
    if args.action == "export" and not args.auto and not args.context_id and not args.revision_id:
        parser.error("export requires --auto, --context-id or --revision-id")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    snapshots, skipped = asyncio.run(read_snapshots(
        args.config, context_ids=args.context_id, revision_ids=args.revision_id, limit=args.limit,
    ))
    if args.action == "list":
        print(json.dumps({"revisions": [snapshot_stats(s) for s in snapshots], "skipped": skipped}, indent=2))
    else:
        corpus = BenchmarkCorpus(cases=select_cases(snapshots, automatic=args.auto))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Refuse replacement so the frozen corpus cannot silently change between runs.
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(corpus.model_dump_json(indent=2))
        print(json.dumps({"output": str(args.output.resolve()), "cases": [
            {"name": case.name, "category": case.category, "revisions": [snapshot_stats(s) for s in case.snapshots]}
            for case in corpus.cases
        ], "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
