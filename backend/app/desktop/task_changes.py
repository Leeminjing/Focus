"""Ordinary main Run changes, projected from immutable tool file versions.

The list carries bounded line statistics; snapshots are fetched only on expansion.
No working-tree diff or tool stdout is used as evidence of this Run's writes.
"""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopThread


task_changes_router = APIRouter()


def line_counts(before, after):
    """Myers distance with the same search limits/line semantics as the UI diff."""
    if len(before.encode("utf-8")) + len(after.encode("utf-8")) > 2 * 1024 * 1024:
        return None
    left = re.findall(r"[^\n]*\n|[^\n]+$", before)
    right = re.findall(r"[^\n]*\n|[^\n]+$", after)
    if len(left) + len(right) > 100000:
        return None
    prefix = 0
    while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
        prefix += 1
    left, right = left[prefix:], right[prefix:]
    suffix = 0
    while suffix < min(len(left), len(right)) and left[-suffix - 1] == right[-suffix - 1]:
        suffix += 1
    if suffix:
        left, right = left[:-suffix], right[:-suffix]
    if not set(left).intersection(right):
        return {"added": len(right), "removed": len(left)}
    frontier, work, entries = {1: 0}, 0, 0
    for distance in range(len(left) + len(right) + 1):
        following = {}
        for diagonal in range(-distance, distance + 1, 2):
            work, entries = work + 1, entries + 1
            if work > 2000000 or entries > 250000:
                return None
            down = diagonal == -distance or (diagonal != distance and frontier[diagonal - 1] < frontier[diagonal + 1])
            x = frontier[diagonal + 1] if down else frontier[diagonal - 1] + 1
            y = x - diagonal
            while x < len(left) and y < len(right) and left[x] == right[y]:
                x, y, work = x + 1, y + 1, work + 1
                if work > 2000000:
                    return None
            following[diagonal] = x
            if x >= len(left) and y >= len(right):
                added = (distance + len(right) - len(left)) // 2
                return {"added": added, "removed": distance - added}
        frontier = following
    return None


def aggregate_versions(rows):
    """Collapse ordered captures to first-before / last-after, per slot and path."""
    files = {}
    for row in rows:
        payload = row.payload
        if payload.get("format") != "file_version_v1":
            continue
        identity = (payload.get("slot_id"), payload["path"])
        item = files.get(identity)
        if item is None:
            item = {"path": payload["path"], "slot_id": payload.get("slot_id"),
                    "first_key": row.result_key, "before": payload["before"], "continuous": True}
            files[identity] = item
        else:
            previous, following = item["after"], payload["before"]
            if "sha256" not in previous or "sha256" not in following or previous["sha256"] != following["sha256"]:
                item["continuous"] = False
        item.update(last_key=row.result_key, after=payload["after"])
    result = []
    for item in files.values():
        before, after = item["before"], item["after"]
        if "sha256" in before and "sha256" in after and before["sha256"] == after["sha256"] and item["continuous"]:
            continue
        item["operation"] = "deleted" if after.get("exists") is False else "created" if before.get("exists") is False else "modified"
        item["reason"] = None
        if not item["continuous"]:
            item["reason"] = "discontinuous_versions"
        elif not before.get("available") or not after.get("available"):
            item["reason"] = next((side.get("reason", "unavailable") for side in (before, after) if not side.get("available")), "unavailable")
        counts = line_counts(before["text"], after["text"]) if not item["reason"] else None
        if counts is None and not item["reason"]:
            item["reason"] = "diff_limit"
        item.update(counts or {"added": None, "removed": None})
        item["redacted"] = bool(before.get("redacted") or after.get("redacted"))
        result.append(item)
    return result


async def owning_run(session, task_id, run_id):
    task = await session.get(DesktopThread, task_id)
    run = await session.get(DesktopRun, run_id)
    if task is None or run is None or run.task_id != task_id or run.agent_id != f"main:{task_id}" or run.kind != "main" or run.loop_id:
        raise HTTPException(404, "普通会话运行不存在")
    return run


@task_changes_router.get("/tasks/{task_id}/changes")
async def task_changes(task_id: str, request: Request):
    async with request.app.state.desktop_service.session_factory() as session:
        if await session.get(DesktopThread, task_id) is None:
            raise HTTPException(404, "任务不存在")
        run = await session.scalar(select(DesktopRun).where(
            DesktopRun.task_id == task_id, DesktopRun.agent_id == f"main:{task_id}", DesktopRun.kind == "main"
        ).order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc()).limit(1))
        if run is None or run.loop_id:
            return {"run_id": None, "files": []}
        rows = (await session.scalars(select(DesktopDomainResult).where(
            DesktopDomainResult.run_id == run.run_id, DesktopDomainResult.context_id == task_id,
            DesktopDomainResult.kind == "artifact"
        ).order_by(DesktopDomainResult.created_at, DesktopDomainResult.result_key))).all()
        rows = [row for row in rows if row.payload.get("slot_id") == (run.workspace_anchor or {}).get("slot_id")]
        files = await asyncio.to_thread(aggregate_versions, rows)
        return {"run_id": run.run_id, "status": run.status,
                "files": [{key: value for key, value in file.items() if key not in {"before", "after", "continuous"}} for file in files]}


@task_changes_router.get("/tasks/{task_id}/changes/{run_id}/{first_key}/{last_key}")
async def change_detail(task_id: str, run_id: str, first_key: str, last_key: str, request: Request):
    async with request.app.state.desktop_service.session_factory() as session:
        run = await owning_run(session, task_id, run_id)
        versions = [await session.get(DesktopDomainResult, key) for key in (first_key, last_key)]
        for row in versions:
            if row is None or row.run_id != run_id or row.context_id != task_id or row.kind != "artifact" or row.payload.get("format") != "file_version_v1" or row.payload.get("slot_id") != (run.workspace_anchor or {}).get("slot_id"):
                raise HTTPException(404, "文件改动版本不存在")
        first, last = versions
        if (first.payload["slot_id"], first.payload["path"]) != (last.payload["slot_id"], last.payload["path"]):
            raise HTTPException(409, "文件改动版本不匹配")
        return {"path": first.payload["path"], "before": first.payload["before"], "after": last.payload["after"],
                "first_key": first_key, "last_key": last_key, "run_id": run_id}
