"""Production JS retry -> HTTP derive -> frozen revision validation -> model projection."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.contexts.service import ContextService
from backend.app.desktop.conversations.inputs import ConversationInputs
from backend.app.desktop.conversations.message_reference import project_message_reference
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.api.routes import desktop_router
from backend.tests.http_asgi_bridge import ASGIHTTPBridge
from focus.config.app_config import AppConfig


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("action", ["retry", "edit", "regenerate"])
def test_frontend_reference_retry_passes_real_backend_validation(action, monkeypatch):
    saver = InMemorySaver()

    async def graph(_self):
        return create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])

    monkeypatch.setattr(ContextService, "make_state_graph", graph)
    app = FastAPI()
    app.include_router(desktop_router)
    source = uuid.uuid4().hex
    selected = dict(task_id=source, message_id="source-answer", text="Redis 🔒", start=0,
                    end=8, offset_basis="rendered_text_utf16")
    calls = []

    with TestClient(app) as client:
        async def setup():
            engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            contexts = ContextService(sessions, saver, AppConfig.model_validate({"models": [{
                "name": "test", "display_name": "test", "base_url": "http://test",
                "use": "x:y", "model": "test", "api_key": "test",
                "context_window": 131072,
            }]}))
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=source, path=f"/tmp/{source}", display_name="test"))
                await session.flush()
                session.add(DesktopThread(task_id=source, thread_id=source, workspace_id=source, title="source"))
                session.add(DesktopThread(task_id="other-" + source[:24], thread_id="other-" + source,
                    workspace_id=source, title="unrelated"))
            seed = await graph(None)
            seed.checkpointer = saver
            early = await seed.aupdate_state({"configurable": {"thread_id": source}}, {"messages": [
                HumanMessage(content="first question", id="source-question"),
                AIMessage(content="Redis 🔒", id="source-answer"),
            ]})
            await seed.aupdate_state({"configurable": {"thread_id": source}}, {"messages": [
                HumanMessage(**project_message_reference(
                    {"content": "follow up", "id": "follow-question"}, selected)),
                AIMessage(content="follow answer", id="follow-answer"),
            ]}, as_node="model")

            async def start_main_run(**kwargs):
                calls.append(kwargs)
                projected = project_message_reference({"content": kwargs["message"], "id": "replayed"}, kwargs["message_reference"])
                return SimpleNamespace(payload={"run_id": uuid.uuid4().hex, "task_id": kwargs["task_id"],
                    "status": "pending", "kind": "main", "message": kwargs["message"],
                    "message_reference": projected["additional_kwargs"]["message_reference"]})

            async def run_by_idempotency(_key):
                return None

            app.state.desktop_service = SimpleNamespace(contexts=contexts, session_factory=sessions,
                start_main_run=start_main_run, run_by_idempotency=run_by_idempotency, notify_run_dispatch=lambda: None)
            app.state.desktop_service.conversation_inputs = ConversationInputs(app.state.desktop_service)
            return engine, early["configurable"]["checkpoint_id"]

        engine, early_checkpoint = client.portal.call(setup)
        try:
            with ASGIHTTPBridge(client) as bridge:
                result = subprocess.run(["node", str(Path(__file__).parents[2] / "desktop" /
                    "tests/e2e/message-reference-retry.integration.cjs"), bridge.url, source, action],
                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            receipt = json.loads(result.stdout)
            assert receipt["task_id"] != source
            assert receipt["message_reference"] == selected
            assert calls[0]["message_reference"] == selected
            if action == "retry":
                branch = receipt["task_id"]
                body = {"message": "follow up", "message_reference": selected}
                # Neither a sibling in the same workspace nor an altered quote is authorized.
                for target, quote in [("other-" + source[:24], selected),
                                      (branch, {**selected, "text": "Other 🔒"}),
                                      (branch, {**selected, "message_id": "forged"}),
                                      (branch, {**selected, "task_id": "unrelated"}),
                                      (branch, {**selected, "start": 1, "end": 9})]:
                    invalid = {**body, "message_reference": quote}
                    assert client.post(f"/desktop/api/tasks/{target}/main/runs", json=invalid).status_code == 422
                    assert client.post(f"/desktop/api/tasks/{target}/main/inputs", json={"mode": "queue", "run": invalid},
                        headers={"Idempotency-Key": uuid.uuid4().hex}).status_code == 422

                snapshot = client.get(f"/desktop/api/contexts/{source}/snapshot").json()
                # A quotation added after a declared checkpoint cannot be admitted from latest history.
                early_branch = client.post("/desktop/api/contexts/derive", json={"title": "early",
                    "sources": [{"context_id": source, "checkpoint_id": early_checkpoint}], "messages": []}).json()
                assert client.post(f"/desktop/api/tasks/{early_branch['context_id']}/main/runs", json=body).status_code == 422

                # Updating the source must not invalidate the branch's original frozen quotation.
                async def change_source():
                    seed = await graph(None)
                    seed.checkpointer = saver
                    await seed.aupdate_state({"configurable": {"thread_id": source}}, {"messages": [
                        HumanMessage(content="changed without quotation", id="follow-question"),
                    ]}, as_node="model")
                    async with app.state.desktop_service.session_factory.begin() as session:
                        session.add(DesktopRun(run_id=branch, task_id=branch, agent_id=f"main:{branch}",
                            kind="main", status="running"))

                client.portal.call(change_source)
                assert client.post(f"/desktop/api/tasks/{branch}/main/runs", json=body).status_code == 200
                for mode in ("queue", "steer"):
                    queued = client.post(f"/desktop/api/tasks/{branch}/main/inputs", json={"mode": mode,
                        "run": body, **({"target_run_id": branch} if mode == "steer" else {})},
                        headers={"Idempotency-Key": uuid.uuid4().hex})
                    assert queued.status_code == 200, queued.text
                    assert queued.json()["run"]["message_reference"] == selected

                # Retry an inherited question again: its origin remains the original source.
                inherited = client.post("/desktop/api/contexts/derive", json={"title": "inherited",
                    "sources": [{"context_id": source, "checkpoint_id": snapshot["checkpoint_id"]}],
                    "messages": snapshot["messages"]})
                assert inherited.status_code == 200, inherited.text
                with ASGIHTTPBridge(client) as bridge:
                    nested = subprocess.run(["node", str(Path(__file__).parents[2] / "desktop" /
                        "tests/e2e/message-reference-retry.integration.cjs"), bridge.url, inherited.json()["context_id"],
                        "retry", source], capture_output=True, text=True, encoding="utf-8", timeout=30)
                assert nested.returncode == 0, nested.stdout + nested.stderr
                assert json.loads(nested.stdout)["message_reference"] == selected
        finally:
            client.portal.call(engine.dispose)
