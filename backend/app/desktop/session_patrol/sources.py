"""本文件对外提供 PatrolSourceResolver 的来源目录、只读预览与原子导入协调。

输入为草稿身份、期望版本、精确来源与稳定行/目标锚点；输出为只读分页结果或已提交草稿及本次插入增量。
具体工作流为在独立读取会话解析/冻结候选，再锁定草稿校验 CAS 和锚点，单次提交正文与冻结引用；旧追加调用兼容。
示例：await resolver.import_into(draft_id, {"kind":"context", "revision_id":"r1", "draft_revision":2})。
"""
from copy import deepcopy
from fastapi import HTTPException
from backend.app.desktop.models import PatrolDraft
from .source_reader import PatrolSourceReader
from .source_browse import PatrolSourceBrowser
from .source_projection import freeze_authoring_sources, source_fingerprint
from .repository import DraftRepository


class PatrolSourceResolver:
    def __init__(self, host):
        self._host = host
        self._reader = PatrolSourceReader(host)
        self._browser = PatrolSourceBrowser(host, self._reader)

    async def catalog(self, **options):
        return await self._browser.catalog(**options)

    async def preview(self, draft_id, request):
        return await self._browser.preview(draft_id, request)

    async def import_into(self, draft_id: str, request: dict):
        async with self._host.session_factory() as read_session:
            candidate = await read_session.get(PatrolDraft, draft_id)
            self._require_draft(candidate, request)
            records, source = await self._reader.read(read_session, request)
        entries, frozen = self._prepare(records, source, request)
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id, with_for_update=True)
            self._require_draft(draft, request)
            document = DraftRepository.document(draft)
            index = self._insertion_index(document, request.get("before_entry_id"))
            document.entries[index:index] = entries
            draft.authoring_document = document.model_dump(mode="json")
            draft.frozen_sources = {**deepcopy(draft.frozen_sources or {}), **frozen}
            draft.draft_revision += 1
            await session.commit()
            result = self._host._draft_payload(draft)
            result["inserted_entries"] = [entry.model_dump(mode="json") for entry in entries]
            result["inserted_entry_ids"] = [entry.entry_id for entry in entries]
            result["inserted_sources"] = frozen
            return result

    @staticmethod
    def _require_draft(draft, request):
        if draft is None or draft.status != "editing":
            raise HTTPException(404, "草稿不存在")
        if request.get("draft_revision") != draft.draft_revision:
            raise HTTPException(409, {"code": "draft_revision_conflict", "message": "草稿已有新版本，本地编辑已保留"})

    @staticmethod
    def _prepare(records, source, request):
        historical = bool(request.get("include_historical", request.get("include_historical_runtime", False)))
        assembly = any(key in request for key in ("selected_row_ids", "source_fingerprint", "before_entry_id"))
        if assembly:
            if not all(key in request for key in ("selected_row_ids", "source_fingerprint", "before_entry_id")):
                raise HTTPException(422, {"code": "invalid_source_selection", "message": "请提交来源选择、指纹和插入位置"})
            if source_fingerprint(records, source, include_historical=historical) != request["source_fingerprint"]:
                raise HTTPException(409, {"code": "source_changed", "message": "来源已变化，请刷新预览后重新确认"})
        try:
            return freeze_authoring_sources(records, source, selected=request.get("message_ids") if not assembly else None,
                selected_rows=request.get("selected_row_ids") if assembly else None, include_historical=historical)
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_source_selection", "message": str(exc)}) from exc

    @staticmethod
    def _insertion_index(document, anchor):
        if anchor is None:
            return len(document.entries)
        index = next((index for index, entry in enumerate(document.entries) if entry.entry_id == anchor), None)
        if index is None:
            raise HTTPException(409, {"code": "anchor_missing", "message": "插入位置已不存在，请重新选择位置"})
        return index
