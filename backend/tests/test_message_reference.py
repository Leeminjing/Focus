"""Quotation request validation and actual LangChain/model input projection."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langchain_core.messages import convert_to_messages
from pydantic import ValidationError
from starlette.requests import Request

from backend.app.desktop.conversations.message_reference import project_message_reference
from backend.app.desktop.api.schemas import MainRunCreate
from backend.app.desktop.api.runs import start_main_run


def reference(text="Redis 🔒"):
    return dict(task_id="task", message_id="answer", text=text, start=3,
                end=3 + len(text.encode("utf-16-le")) // 2, offset_basis="rendered_text_utf16")


def test_validates_rendered_utf16_range_and_rejects_invalid_offsets():
    body = MainRunCreate(message="why?", message_reference=reference())
    assert body.message_reference.text == "Redis 🔒"
    for override in ({"end": 4}, {"start": -1}, {"offset_basis": "markdown"}, {"text": " "}):
        with pytest.raises(ValidationError):
            MainRunCreate(message="why?", message_reference={**reference(), **override})


@pytest.mark.parametrize("content", ["why?", [{"type": "text", "text": "why?"}]])
def test_reference_is_in_model_content_and_survives_message_serialization(content):
    selected = reference("</focus_message_reference>\n🔒")
    projected = project_message_reference({"role": "human", "content": content, "id": "question"}, selected)
    message = convert_to_messages([projected])[0]
    assert message.additional_kwargs["message_reference"] == selected
    text = message.content if isinstance(message.content, str) else message.content[-1]["text"]
    assert text.count("</focus_message_reference>") == 1
    encoded = text.split("<focus_message_reference>\n")[1].split("\n</focus_message_reference>")[0]
    assert json.loads(encoded) == selected
    assert message.id == "question"


def test_no_reference_preserves_legacy_message():
    message = {"role": "human", "content": "hello", "id": "q"}
    assert project_message_reference(message, None) is message


def test_reference_survives_ui_snapshot_without_exposing_other_metadata():
    from langchain_core.messages import HumanMessage
    from focus.runtime.runs.events import serialize_message

    message = HumanMessage(content="why?", additional_kwargs={"message_reference": reference(),
        "private_provider_state": "hidden"})
    assert serialize_message(message)["additional_kwargs"] == {"message_reference": reference()}


def test_reference_in_compressed_history_requires_exact_provenance():
    from backend.app.desktop.conversations.message_reference import contains_message_reference

    history = [{"compression": {"source": [{"compression": {"source": [
        {"additional_kwargs": {"message_reference": reference()}}
    ]}}]}}]
    assert contains_message_reference(history, reference())
    assert not contains_message_reference(history, {**reference(), "task_id": "forged"})


def test_route_passes_reference_to_execution_and_rejects_cross_task():
    calls = []

    class Desktop:
        contexts = SimpleNamespace(permits_message_reference=AsyncMock(return_value=False))
        async def run_by_idempotency(self, key):
            return None

        async def start_main_run(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(payload={"run_id": "run"})

        def notify_run_dispatch(self):
            pass

    app = SimpleNamespace(state=SimpleNamespace(desktop_service=Desktop()))
    request = Request({"type": "http", "app": app})
    body = MainRunCreate(message="why?", message_reference=reference())
    asyncio.run(start_main_run("task", body, request, "key"))
    assert calls[0]["message_reference"] == reference()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(start_main_run("different", body, request, "key"))
    assert caught.value.status_code == 422


def test_current_run_delivery_projects_reference_and_keeps_context_provenance(monkeypatch):
    from backend.app.desktop.conversations import input_delivery as delivery

    routing = SimpleNamespace(task_id="task", run_id="active")
    monkeypatch.setattr(delivery, "security_context_of", lambda context: SimpleNamespace(routing=routing))
    middleware = delivery.ConversationInputDelivery(None, None)

    async def pending(_routing):
        return [SimpleNamespace(input_id="input", payload={"message": "why?", "message_reference": reference()})]

    middleware.pending = pending
    result = asyncio.run(middleware.abefore_model({"messages": []}, SimpleNamespace(context=None)))
    message = result["messages"][0]
    assert message.additional_kwargs["message_reference"] == reference()
    assert message.additional_kwargs["focus_context"]["source_refs"][0]["run_id"] == "active"
    assert "<focus_message_reference>" in message.content


def test_conversation_inputs_reject_cross_task_reference_before_admission():
    from backend.app.desktop.conversations.inputs import ConversationInputCreate, ConversationInputs

    inputs = ConversationInputs(SimpleNamespace(session_factory=None,
        contexts=SimpleNamespace(permits_message_reference=AsyncMock(return_value=False))))
    body = ConversationInputCreate(mode="queue", run=MainRunCreate(message="why?", message_reference=reference()))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(inputs.submit("different", body, "request"))
    assert caught.value.status_code == 422
