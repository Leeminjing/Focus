"""本文件对外提供 save_patrol_document 和 save_patrol_draft，用当前文档及CAS合同保存测试草稿。

输入为真实HTTP client、当前草稿版本、完整作者文档或旧消息测试载荷、装备及会话头；输出为真实保存响应。
具体工作流为复用生产legacy_document的无损读适配，将v3 authoring_document和draft_revision提交当前API；
不重新启用旧写接口、不伪造预览token、来源或模型准备证明。示例：save_patrol_draft(client, draft,
headers=session, system_prompt="检查", history_messages=[], final_human_message="执行", equipment=equipment)。
"""

from backend.app.desktop.session_patrol.contracts import legacy_document


def save_patrol_draft(client, draft, *, headers, system_prompt, history_messages, final_human_message, equipment):
    document = legacy_document(system_prompt, history_messages, final_human_message)
    return save_patrol_document(client, draft, headers=headers,
        authoring_document=document.model_dump(mode="json"), equipment=equipment)


def save_patrol_document(client, draft, *, headers, authoring_document, equipment):
    return client.put(f"/desktop/api/drafts/{draft['draft_id']}", headers=headers, json={
        "authoring_document": authoring_document, "draft_revision": draft["draft_revision"],
        "equipment": equipment,
    })
