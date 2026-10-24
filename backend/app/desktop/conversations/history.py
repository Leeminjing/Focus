"""Read-only history search over conversation displays and durable material names.

Search and the reader use the same message keys, including retained compression
sources. Deleted contexts are excluded. No graph, draft or execution is started.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.runs.evidence.models import DesktopDomainResult
from backend.app.desktop.persistence.models import DesktopMaterial, DesktopThread, DesktopWorkspace, RunMaterialBinding

logger = logging.getLogger(__name__)


def message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content if isinstance(block, (str, dict))
        )
    else:
        text = ""
    # Tool arguments contain filenames even when the tool response is empty.
    calls = message.get("tool_calls") or []
    return "\n".join([text, *[str(call.get("args", call.get("function", {}).get("arguments", "")))
                              for call in calls if isinstance(call, dict)]]).strip()


def history_entries(messages: list[dict], prefix: str = "message", depth: int = 0) -> list[dict]:
    entries = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        key = f"{prefix}:{index}"
        entries.append({
            "key": key, "message_id": message.get("id") or message.get("message_id"),
            "role": message.get("role") or message.get("type") or "message",
            "text": message_text(message), "retained_source": depth > 0,
        })
        sources = (message.get("compression") or {}).get("source")
        if isinstance(sources, list) and depth < 8:
            entries.extend(history_entries(sources, key, depth + 1))
    return entries


def snippet(text: str, query: str) -> str:
    # Preserve original case and center the preview on the literal match.
    position = text.lower().find(query.lower())
    start = max(0, position - 60)
    end = min(len(text), max(position, 0) + len(query) + 100)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


class HistorySearchService:
    def __init__(self, session_factory, contexts):
        self.session_factory = session_factory
        self.contexts = contexts

    async def _catalog(self, context_id: str | None = None) -> tuple[list[dict], dict[str, list[dict]]]:
        async with self.session_factory() as session:
            statement = select(DesktopThread, DesktopWorkspace).join(
                DesktopWorkspace, DesktopWorkspace.workspace_id == DesktopThread.workspace_id
            ).where(DesktopThread.deleted_at.is_(None)).order_by(
                DesktopThread.updated_at.desc(), DesktopThread.task_id
            )
            if context_id is not None:
                statement = statement.where(DesktopThread.task_id == context_id)
            rows = (await session.execute(statement)).all()
            tasks = [{
                "context_id": task.task_id, "title": task.title,
                "workspace_name": workspace.display_name,
                "lifecycle": "archived" if task.archived_at else "active",
            } for task, workspace in rows]
            ids = [task["context_id"] for task in tasks]
            materials: dict[str, list[dict]] = {task_id: [] for task_id in ids}
            if not ids:
                return tasks, materials
            bindings = (await session.scalars(select(RunMaterialBinding).where(
                RunMaterialBinding.task_id.in_(ids)
            ).order_by(RunMaterialBinding.created_at, RunMaterialBinding.ordinal))).all()
            for binding in bindings:
                materials[binding.task_id].append({
                    "key": f"file:{binding.binding_id}", "message_id": binding.message_id,
                    "path": binding.relative_path_snapshot, "note": binding.note, "kind": "input",
                })
            current = (await session.scalars(select(DesktopMaterial).where(
                DesktopMaterial.task_id.in_(ids)
            ).order_by(DesktopMaterial.created_at, DesktopMaterial.material_id))).all()
            for material in current:
                if not any(item["path"] == material.relative_path for item in materials[material.task_id]):
                    materials[material.task_id].append({
                        "key": f"file:{material.material_id}", "message_id": None,
                        "path": material.relative_path, "note": "", "kind": "material",
                    })
            # Durable artifact observations survive conversation compression and
            # do not require the file to still exist in the current workspace.
            artifacts = (await session.execute(select(
                DesktopDomainResult.result_key, DesktopDomainResult.context_id,
                DesktopDomainResult.payload["path"].astext,
            ).where(
                DesktopDomainResult.context_id.in_(ids), DesktopDomainResult.kind == "artifact",
            ).order_by(DesktopDomainResult.created_at, DesktopDomainResult.result_key))).all()
            seen_artifacts = set()
            for result_key, task_id, path in artifacts:
                if path and (task_id, path) not in seen_artifacts:
                    seen_artifacts.add((task_id, path))
                    materials[task_id].append({
                        "key": f"file:{result_key}", "message_id": None,
                        "path": path, "note": "", "kind": "artifact",
                    })
        return tasks, materials

    async def read(self, context_id: str) -> dict[str, Any]:
        tasks, materials = await self._catalog(context_id)
        if not tasks:
            raise HTTPException(404, "历史任务不存在或已删除")
        snapshot = await self.contexts.live_conversation(context_id)
        return {**tasks[0], "entries": history_entries(snapshot["messages"]),
                "materials": materials[context_id], "read_only": True}

    async def search(self, query: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"results": [], "has_more": False, "unavailable_context_ids": []}
        tasks, materials = await self._catalog()
        results, unavailable = [], []
        needle = query.lower()

        async def scan(task):
            task_id = task["context_id"]
            hits = []
            metadata = f"{task['title']} {task_id} {task['workspace_name']}"
            if needle in metadata.lower():
                hits.append({**task, "kind": "task", "key": "", "snippet": snippet(metadata, query)})
            try:
                snapshot = await self.contexts.live_conversation(task_id)
                entries = history_entries(snapshot["messages"])
            except Exception:
                logger.warning("Cannot read history for context %s", task_id, exc_info=True)
                unavailable.append(task_id)
                entries = []
            for entry in reversed(entries):
                if needle in entry["text"].lower():
                    hits.append({**task, "kind": "message", "key": entry["key"],
                                 "message_id": entry["message_id"], "snippet": snippet(entry["text"], query)})
            for material in materials[task_id]:
                text = material["path"] + "\n" + material["note"]
                if needle in text.lower():
                    entry = next((item for item in entries if item["message_id"] == material["message_id"]), None) if material["message_id"] else None
                    hits.append({**task, "kind": "file", "key": entry["key"] if entry else material["key"],
                                 "message_id": material["message_id"], "snippet": snippet(text, query)})
            return hits

        # Bound simultaneous checkpoint reads and stop once the next page exists.
        for start in range(0, len(tasks), 4):
            for hits in await asyncio.gather(*(scan(task) for task in tasks[start:start + 4])):
                results.extend(hits)
            if len(results) > offset + limit:
                break
        return {"results": results[offset:offset + limit], "has_more": len(results) > offset + limit,
                "unavailable_context_ids": unavailable}
