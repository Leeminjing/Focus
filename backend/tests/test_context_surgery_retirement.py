"""Retired manual surgery cannot mutate Contexts; Patrol read/control routes remain live."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.desktop.api.contexts import router as contexts_router
from backend.app.desktop.api.patrol import router as patrol_router


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/contexts/derive",
            {
                "title": "branch",
                "sources": [{"context_id": "source", "checkpoint_id": "c1"}],
                "messages": [],
            },
        ),
        (
            "put",
            "/contexts/source/definition",
            {"messages": [{"role": "human", "content": "replacement"}]},
        ),
        (
            "post",
            "/contexts/source/projection/decision",
            {
                "decision": "accept",
                "definition_hash": "a" * 64,
                "projection_hash": "b" * 64,
            },
        ),
    ],
)
def test_manual_context_surgery_has_no_http_handler(method, path, body):
    app = FastAPI()
    app.include_router(contexts_router)
    # There is intentionally no service: a removed route must never enter application work.
    with TestClient(app) as client:
        response = client.request(method, path, json=body)
    assert response.status_code in {404, 405}


def test_current_snapshot_and_automatic_curation_controls_remain_available():
    contexts = SimpleNamespace(
        snapshot=AsyncMock(return_value={"context_id": "source", "messages": []})
    )
    curator = SimpleNamespace(
        detail=AsyncMock(
            return_value={"agent_id": "curator", "control_state": "following"}
        ),
        set_tracking_state=AsyncMock(return_value={"control_state": "paused"}),
    )
    app = FastAPI()
    app.include_router(contexts_router)
    app.include_router(patrol_router)
    app.state.desktop_service = SimpleNamespace(
        contexts=contexts, context_patrol=curator
    )

    with TestClient(app) as client:
        snapshot = client.get("/contexts/source/snapshot")
        detail = client.get("/agents/curator/context-curation")
        control = client.put(
            "/agents/curator/context-curation/state", json={"state": "paused"}
        )

    assert snapshot.status_code == detail.status_code == control.status_code == 200
    assert snapshot.json()["context_id"] == "source"
    assert detail.json()["control_state"] == "following"
    assert control.json()["control_state"] == "paused"
    contexts.snapshot.assert_awaited_once_with("source", None)
    curator.detail.assert_awaited_once_with("curator", limit=50, before=None)
    curator.set_tracking_state.assert_awaited_once_with("curator", "paused")
