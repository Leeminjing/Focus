"""本文件对外提供 PatrolSourceBrowser 的 catalog、preview 只读来源端口。

输入为来源类型、精确版本、查询/有界游标以及调用方草稿身份；输出为分页目录或稳定投影行/完整行详情。
具体工作流为读取精确来源，复用唯一投影/指纹，验证游标范围并只序列化命中页；不保存草稿、不冻结引用或执行模型/工具。
示例：await browser.preview(draft_id, {"kind": "context", "revision_id": "r1", "limit": 50})。
"""
import base64
from datetime import datetime
import json

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from backend.app.desktop.models import DesktopThread, DesktopRun, PatrolDraft
from backend.app.desktop.context_evolution.models import ContextRevision
from focus.history import content_hash
from .source_projection import project_authoring_sources, source_fingerprint


def _encode_cursor(scope, after):
    return base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": after}).encode()).decode()


def _decode_cursor(cursor, scope):
    if not cursor:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor))
        if value["scope"] != scope:
            raise ValueError("范围不同")
        return value["after"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, {"code": "invalid_source_cursor", "message": "来源或查询已变化，请重新浏览"}) from exc


class PatrolSourceBrowser:
    def __init__(self, host, reader):
        self._host, self._reader = host, reader

    async def catalog(self, *, kind="context", query="", cursor=None, limit=None):
        if kind not in {"context", "patrol"}:
            raise HTTPException(422, "目录仅支持 Context 或 Patrol")
        scope = content_hash(["catalog-v1", kind, query])
        after = _decode_cursor(cursor, scope)
        async with self._host.session_factory() as session:
            if limit is None:
                contexts = await self._contexts(session)
                branches = await self._branches(session)
                grouped = {}
                for row in contexts:
                    group = grouped.setdefault(row.context_id, {"context_id": row.context_id, "title": row.title, "revisions": []})
                    group["revisions"].append({"revision_id": row.revision_id, "generation": row.generation, "checkpoint_id": row.checkpoint_id})
                return {"contexts": list(grouped.values()), "branches": [dict(row._mapping) for row in branches]}
            limit = max(1, min(int(limit), 100))
            rows = await (self._contexts(session, query, after, limit + 1) if kind == "context"
                          else self._branches(session, query, after, limit + 1))
            page = rows[:limit]
            items = []
            for row in page:
                if kind == "context":
                    ref = {"kind": kind, "context_id": row.context_id, "revision_id": row.revision_id}
                    label = f"{row.title} · R{row.generation}"
                else:
                    ref = {"kind": kind, "run_id": row.run_id, "checkpoint_id": row.checkpoint_id}
                    label = f"{row.title} · {row.status} · {row.run_id[:8]}"
                items.append({"source_ref": ref, "label": label, "available": kind == "context" or bool(row.checkpoint_id)})
            tail = page[-1] if page else None
            next_cursor = (_encode_cursor(scope, [tail.created_at.isoformat(), tail.revision_id if kind == "context" else tail.run_id])
                           if len(rows) > limit and tail else None)
            return {"items": items, "next_cursor": next_cursor}

    async def _contexts(self, session, query="", after=None, limit=None):
        statement = select(ContextRevision.revision_id, ContextRevision.context_id, ContextRevision.generation,
                           ContextRevision.checkpoint_id, ContextRevision.created_at, DesktopThread.title).join(
                           DesktopThread, DesktopThread.task_id == ContextRevision.context_id).where(
                           DesktopThread.deleted_at.is_(None), ContextRevision.deleted_at.is_(None))
        statement = self._page(statement, ContextRevision, ContextRevision.revision_id, query, after, limit)
        return (await session.execute(statement)).all()

    async def _branches(self, session, query="", after=None, limit=None):
        statement = select(DesktopRun.run_id, DesktopRun.agent_id, DesktopRun.task_id, DesktopRun.execution_thread_id.label("thread_id"),
                           DesktopRun.checkpoint_ns, DesktopRun.final_checkpoint_id.label("checkpoint_id"), DesktopRun.status,
                           DesktopRun.created_at, DesktopThread.title).join(DesktopThread, DesktopThread.task_id == DesktopRun.task_id).where(
                           DesktopRun.kind == "patrol", DesktopThread.deleted_at.is_(None))
        statement = self._page(statement, DesktopRun, DesktopRun.run_id, query, after, limit)
        return (await session.execute(statement)).all()

    @staticmethod
    def _page(statement, model, identity, query, after, limit):
        if query:
            statement = statement.where(DesktopThread.title.icontains(query, autoescape=True))
        if after is not None:
            try:
                timestamp, row_id = datetime.fromisoformat(after[0]), after[1]
                if not isinstance(row_id, str):
                    raise ValueError("无效身份")
            except (ValueError, TypeError, IndexError, KeyError) as exc:
                raise HTTPException(422, "无效来源游标") from exc
            statement = statement.where(or_(model.created_at < timestamp, and_(model.created_at == timestamp, identity < row_id)))
        statement = statement.order_by(model.created_at.desc(), identity.desc())
        return statement.limit(limit) if limit is not None else statement

    async def preview(self, draft_id, request):
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if draft is None or draft.status != "editing":
                raise HTTPException(404, "草稿不存在")
            records, source = await self._reader.read(session, request)
        historical = bool(request.get("include_historical", request.get("include_historical_runtime", False)))
        fingerprint = source_fingerprint(records, source, include_historical=historical)
        rows = project_authoring_sources(records, source, include_historical=historical)
        if request.get("row_id"):
            row = next((row for row in rows if row["source_row_id"] == request["row_id"]), None)
            if row is None:
                raise HTTPException(404, "来源行不存在")
            return {"source_ref": source, "source_fingerprint": fingerprint, "row": self._summary(row, full=True)}
        query = str(request.get("query") or "")
        scope = content_hash([fingerprint, query])
        offset = _decode_cursor(request.get("cursor"), scope) or 0
        if not isinstance(offset, int) or offset < 0:
            raise HTTPException(422, "无效来源游标")
        matched = [row for row in rows if not query or query.casefold() in self._text(row).casefold()]
        limit = max(1, min(int(request.get("limit") or 50), 100))
        page = matched[offset:offset + limit]
        return {"source_ref": source, "source_fingerprint": fingerprint, "rows": [self._summary(row) for row in page],
                "total": len(matched), "excluded_count": sum(not row["eligible"] for row in rows),
                "next_cursor": _encode_cursor(scope, offset + limit) if offset + limit < len(matched) else None}

    @staticmethod
    def _text(row):
        if not row["eligible"]:
            return row["entry"]["kind"] + " " + row["exclusion_reason"]
        payload = row["entry"]["payload"]
        value = payload.get("content", payload.get("output", payload.get("arguments", payload.get("input", payload))))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    @classmethod
    def _summary(cls, row, *, full=False):
        entry, text = row["entry"], cls._text(row)
        result = {"source_row_id": row["source_row_id"], "kind": entry["kind"], "role": entry["payload"].get("role"),
                  "summary": text[:600], "truncated": len(text) > 600, "eligible": row["eligible"],
                  "exclusion_reason": row["exclusion_reason"], "historical_runtime": row["historical_runtime"]}
        if full and row["eligible"]:
            result["entry"] = entry
        return result
