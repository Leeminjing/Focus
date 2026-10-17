"""Conversation links retain the producer of each immutable message, including inherited answers."""
import asyncio
from types import SimpleNamespace as NS

from backend.app.desktop.agent_loop.conversation_query import ContextConversationQueryService
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.models import DesktopRun, DesktopThread


def test_conversation_projects_original_file_sources_across_runs_and_derivation():
    old = {"id": "old", "role": "ai", "content": "[旧报告](report.md)"}
    new = {"id": "new", "role": "ai", "content": "[新报告](report.md)"}
    def revision(key, context, messages, sources=(), run=None):
        return NS(ref=NS(revision_id=key, context_id=context, model_dump=lambda **_: {"revision_id": key}),
                  sources=tuple(NS(source=NS(revision_id=source)) for source in sources),
                  origin_kind="run_settled" if run else "derive", origin_id=run,
                  projection_status=NS(value="valid"), messages=messages)
    revisions = {
        "r1": revision("r1", "c", [old], run="run-one"),
        "r2": revision("r2", "c", [old, new], ["r1"], "run-two"),
        "r3": revision("r3", "derived", [old, new], ["r2"]),
    }
    class Repository:
        async def get_by_id(self, session, key): return revisions[key]
    class Reader:
        async def read(self, session, ref, view): return NS(messages=revisions[ref.revision_id].messages)
        async def read_message_page(self, revision, **_):
            return NS(messages=revision.messages, total=len(revision.messages), start=0, end=len(revision.messages))
    class Session:
        async def get(self, model, key):
            if model is AgentLoop: return NS(workspace_id="w")
            if model is DesktopThread: return NS(workspace_id="w")
            if model is DesktopRun: return NS(run_id=key, task_id="c", workspace_anchor={"slot_id": key + "-slot"})
        async def scalar(self, statement): return NS()
        async def scalars(self, statement): return NS(all=lambda: [])
    service = ContextConversationQueryService(None)
    service._repository, service._reader = Repository(), Reader()
    result = asyncio.run(service.read(Session(), "loop", "derived", revision_id="r3", before=None, limit=30))
    sources = [item.get("file_context") for item in result["messages"]]
    assert sources == [
        {"task_id": "c", "workspace_id": "w", "run_id": "run-one", "slot_id": "run-one-slot"},
        {"task_id": "c", "workspace_id": "w", "run_id": "run-two", "slot_id": "run-two-slot"},
    ]


def test_authored_answer_cannot_claim_a_run_via_model_metadata():
    from backend.app.desktop.agent_loop.file_link_sources import ConversationFileSources
    message = {"id": "made-up", "role": "ai", "content": "[报告](report.md)",
               "file_context": {"run_id": "foreign", "slot_id": "foreign"}}
    revision = NS(ref=NS(revision_id="authored", context_id="c"), origin_kind="derive", origin_id="foreign", sources=())
    sources = ConversationFileSources(NS(), NS(), NS(), "w")
    result = asyncio.run(sources.for_message(revision, message))
    assert "error" in result and "run_id" not in result


def test_conflicting_inherited_sources_never_choose_a_same_named_file():
    from backend.app.desktop.agent_loop.file_link_sources import ConversationFileSources
    message = {"id": "same-id", "role": "ai", "content": "[报告](report.md)"}
    refs = {key: NS(ref=NS(revision_id=key, context_id="c"), origin_kind="run_settled", origin_id=key, sources=())
            for key in ("one", "two")}
    revision = NS(ref=NS(revision_id="merged", context_id="c"), origin_kind="derive", origin_id=None,
                  sources=tuple(NS(source=NS(revision_id=key)) for key in refs))
    class Repository:
        async def get_by_id(self, session, key): return refs[key]
    class Reader:
        async def read(self, *args): return NS(messages=[message])
    class Session:
        async def get(self, model, key):
            return NS(workspace_id="w") if model is DesktopThread else NS(run_id=key, task_id="c", workspace_anchor={"slot_id": key})
    sources = ConversationFileSources(Session(), Repository(), Reader(), "w")
    assert "error" in asyncio.run(sources.for_message(revision, message))


def test_run_source_must_belong_to_the_context_and_workspace():
    from backend.app.desktop.agent_loop.file_link_sources import ConversationFileSources
    message = {"id": "answer", "role": "ai", "content": "[报告](report.md)"}
    revision = NS(ref=NS(revision_id="r", context_id="c"), origin_kind="run_settled", origin_id="run", sources=())
    class Session:
        async def get(self, model, key):
            return NS(workspace_id="foreign") if model is DesktopThread else NS(run_id=key, task_id="c", workspace_anchor={})
    sources = ConversationFileSources(Session(), NS(), NS(), "w")
    assert "error" in asyncio.run(sources.for_message(revision, message))
