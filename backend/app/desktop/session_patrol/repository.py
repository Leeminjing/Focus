"""本文件对外提供 DraftRepository 的草稿 compare-and-swap 和只读适配端口。

输入为事务 session、草稿身份、用户文档及期望版本；输出为新版本草稿或可恢复冲突。
工作流为锁定行、检查版本及旧客户端覆盖、保存自由文档；运行合法性由 compiler 独立判断。
示例：await repository.save(session, draft_id, document, expected_revision=3)。
"""
from fastapi import HTTPException
from backend.app.desktop.models import PatrolDraft
from .contracts import AuthoringDocument, legacy_document


class DraftRepository:
    @staticmethod
    def document(draft: PatrolDraft) -> AuthoringDocument:
        if draft.authoring_document is not None:
            return AuthoringDocument.model_validate(draft.authoring_document)
        return legacy_document(draft.system_prompt, draft.history_messages, draft.final_human_message)

    async def save(self, session, draft_id: str, document: dict | None, *, expected_revision: int | None):
        draft = await session.get(PatrolDraft, draft_id, with_for_update=True)
        if draft is None or draft.status != "editing":
            raise HTTPException(404, "可编辑草稿不存在")
        if document is None and draft.authoring_document is not None:
            raise HTTPException(409, {"code": "client_schema_conflict", "message": "此草稿包含新版字段，请升级编辑器"})
        if document is not None:
            if expected_revision != draft.draft_revision:
                raise HTTPException(409, {"code": "draft_revision_conflict", "draft_revision": draft.draft_revision})
            draft.authoring_document = AuthoringDocument.model_validate(document).model_dump(mode="json")
            draft.draft_revision += 1
        return draft
