"""spatial-patrol DOCX 请求授权边界测试:权限必须显式且非空。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.spatial_patrol import routes


class _MissingSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args):
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "_session_factory", lambda: lambda: _MissingSession())
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/anchors/missing/deploy", {"instruction": "观察"}),
        ("/anchors/missing/deploy", {"instruction": "观察", "permissions": []}),
        ("/anchors/missing/copy", {"x": 0.5, "y": 0.5}),
        ("/anchors/missing/copy", {"x": 0.5, "y": 0.5, "permissions": []}),
        ("/anchors/missing/continue", {"instruction": "继续"}),
        ("/anchors/missing/continue", {"instruction": "继续", "permissions": []}),
    ],
)
def test_spatial_run_requests_reject_missing_or_empty_permissions(client, path, payload):
    assert client.post(path, json=payload).status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/anchors/missing/deploy", {"instruction": "观察", "permissions": ["read"]}),
        ("/anchors/missing/deploy", {"instruction": "删除", "permissions": ["read", "write"]}),
        ("/anchors/missing/copy", {"x": 0.5, "y": 0.5, "permissions": ["read"]}),
        ("/anchors/missing/continue", {"instruction": "继续", "permissions": ["read", "write"]}),
    ],
)
def test_spatial_run_requests_accept_explicit_permissions(client, path, payload):
    assert client.post(path, json=payload).status_code == 404
