"""dsh-eyes 插件的桌面 API 路由(经系统挂载在 /desktop/api/plugin/dsh-eyes 前缀)。

对外提供:
    router — APIRouter:附件字节获取(前端消息区把 attachment_id 引用还原为图片)

工作流:
    GET /attachments/{attachment_id}?thread_id=xxx → 从会话附件索引取回图片字节返回;
    索引无该 id 或字节缺失 → 404。thread_id 由前端(浏览器侧)提供,用于限定会话桶。
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from plugins.dsh_eyes.index import get_index

router = APIRouter()


@router.get("/attachments/{attachment_id}")
async def attachment(attachment_id: str, request: Request, thread_id: str = "") -> Response:
    """返回会话附件图片字节;前端经 img src 直接加载(重启后历史引用可还原)。"""
    data_url = get_index().resolve_data_url(thread_id or "unknown", attachment_id)
    if data_url is None:
        raise HTTPException(404, "附件不存在或不属于当前会话")
    header, sep, payload = data_url.partition(",")
    if not sep:
        raise HTTPException(404, "附件格式非法")
    mime = header.split(";")[0].split(":", 1)[1] if ":" in header else "image/png"
    try:
        data = base64.b64decode(payload)
    except Exception as exc:
        raise HTTPException(404, "附件解码失败") from exc
    return Response(content=data, media_type=mime)
