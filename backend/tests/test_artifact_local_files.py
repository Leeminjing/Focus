"""Local artifact exits use persisted identities, current permissions and slot containment."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.domain_evidence.file_reads import FileVersionReader
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


def test_local_file_authorization_and_containment(tmp_path, monkeypatch):
    async def run():
        report = tmp_path / "报告.pdf"
        report.write_bytes(b"%PDF-report")
        payload = {"slot_id": "slot", "path": report.name, "sha256": "a" * 64, "operation": "created"}
        slot = SimpleNamespace(workspace_id="workspace", root_path=str(tmp_path))
        permissions = {"read"}

        async def access(*_args):
            return SimpleNamespace(permissions=permissions)

        async def read(*_args):
            return payload

        class Session:
            async def get(self, model, _identity):
                return SimpleNamespace(workspace_id="workspace") if model is AgentLoop else slot if model is WorkspaceSlot else None

        monkeypatch.setattr(LoopLiveAccessPolicy, "resolve", access)
        reader = FileVersionReader()
        monkeypatch.setattr(reader, "read", read)
        session = Session()
        result = await reader.local_file(session, "loop", "fact", 1)
        assert result["path"] == str(report.resolve())
        assert result["root_path"] == str(tmp_path.resolve())

        for invalid in ("../outside.txt", "C:/outside.txt", "C:outside.txt", "\\\\server\\share\\file", "file:stream", "..\\outside.txt", "/outside.txt"):
            payload["path"] = invalid
            with pytest.raises(HTTPException) as error:
                await reader.local_file(session, "loop", "fact", 1)
            assert error.value.status_code == 409

        payload["path"] = report.name
        slot.workspace_id = "other"
        with pytest.raises(HTTPException, match="409"):
            await reader.local_file(session, "loop", "fact", 1)
        slot.workspace_id = "workspace"
        permissions.clear()
        with pytest.raises(HTTPException, match="403"):
            await reader.local_file(session, "loop", "fact", 1)
        permissions.add("view_evidence")
        payload["operation"] = "deleted"
        with pytest.raises(HTTPException, match="404"):
            await reader.local_file(session, "loop", "fact", 1)
        payload["operation"] = "modified"
        report.unlink()
        with pytest.raises(HTTPException, match="404"):
            await reader.local_file(session, "loop", "fact", 1)

    asyncio.run(run())
