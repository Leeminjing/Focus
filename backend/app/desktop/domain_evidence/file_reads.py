"""本文件提供 FileVersionReader 的历史预览和普通输入引用校验。

输入为持久 Fact 修订和 Artifact 身份；输出为同工作区的不可变快照或 404/409。
工作流验证 Fact→证据→领域结果→Run slot，不读取当前磁盘，不授予完成核验权限。
示例：await FileVersionReader().read(session, loop_id, fact_id, revision)。
"""

from pathlib import PurePosixPath
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRevision
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun


class FileVersionReader:
    async def read(self, session, loop_id, fact_id, revision=None):
        fact = await session.get(LoopFact, fact_id)
        if fact is None or fact.loop_id != loop_id:
            raise HTTPException(404, "文件事实不存在")
        if fact.fact_type != "artifact":
            return None
        selected = revision or fact.current_revision
        row = await session.scalar(select(LoopFactRevision).where(
            LoopFactRevision.fact_id == fact_id, LoopFactRevision.revision == selected))
        if row is None:
            if revision is None and not any(item.get("metadata", {}).get("file") for item in fact.evidence):
                return None
            raise HTTPException(404, "文件事实版本不存在")
        file = next((item.get("metadata", {}).get("file") for item in row.evidence
            if item.get("kind") == "artifact" and item.get("metadata", {}).get("file")), None)
        if file is None:
            return None
        source = await session.get(DesktopDomainResult, file["result_key"])
        run = await session.get(DesktopRun, source.run_id) if source else None
        if source is None or source.kind != "artifact" or source.loop_id != loop_id or run is None:
            raise HTTPException(404, "文件版本来源不可用")
        payload = source.payload
        path = PurePosixPath(payload.get("path", ""))
        if (payload.get("format") != "file_version_v1" or path.is_absolute() or ".." in path.parts
                or payload.get("slot_id") != (run.workspace_anchor or {}).get("slot_id")):
            raise HTTPException(409, "文件版本来源不一致")
        return {"fact_id": fact_id, "revision": selected, "result_key": source.result_key, **payload}

    async def validate_reference(self, session, workspace_id, reference):
        fact = await session.get(LoopFact, reference.fact_id)
        loop = await session.get(AgentLoop, fact.loop_id) if fact else None
        if loop is None or loop.workspace_id != workspace_id:
            raise HTTPException(409, "引用文件不属于当前工作区")
        from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy

        access = await LoopLiveAccessPolicy().resolve(session, loop.loop_id)
        if not access.permissions.intersection({"read", "view_evidence"}):
            raise HTTPException(403, "当前无权引用文件证据")
        version = await self.read(session, loop.loop_id, reference.fact_id, reference.revision)
        if version is None or any(version.get(key) != getattr(reference, key)
            for key in ("result_key", "slot_id", "sha256", "path")):
            raise HTTPException(409, "引用文件版本不一致")
