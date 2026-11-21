"""普通会话不能通过公开恢复接口提交消息范围编辑。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.desktop.api.runs import router


@pytest.mark.parametrize("decision", ["apply", "cancel"])
def test_public_resume_rejects_compression_before_internal_execution(decision):
    app = FastAPI()
    app.include_router(router, prefix="/desktop/api")
    service = SimpleNamespace(resume_run=AsyncMock())
    app.state.desktop_service = service
    with TestClient(app) as client:
        response = client.post("/desktop/api/threads/context/runs/resume", json={
            "resume": {"type": "compression", "decision": decision,
                       "ranges": [{"source_ids": ["message"], "replacement": "manual rewrite"}]},
        })
    assert response.status_code == 422
    service.resume_run.assert_not_awaited()
