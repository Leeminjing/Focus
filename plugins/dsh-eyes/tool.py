"""dsh-eyes 的 view_image 工具(对齐上游 dsh-eyes 全部行为)。

输入优先级(对齐 dsh-eyes,非互斥):attachment_ids(非空)> attachment_id > image_path;
全部缺省 → 「view_image 错误:请提供 attachment_ids、attachment_id 或 image_path。」

结果格式(对齐 dsh-eyes):
    成功:「【图片】描述」(单图/文件)或「【图片N】描述」(多图),空文本 → （无内容）
    失败:「【图片】错误:…」/「【图片N】错误:…」;单分支直接返回「view_image 错误:…」
"""

import base64
import logging
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

@tool
async def view_image(
    attachment_id: str | None = None,
    attachment_ids: list[str] | None = None,
    image_path: str | None = None,
    prompt: str | None = None,
    runtime: ToolRuntime = None,
) -> str:
    """调用视觉模型查看图片:描述画面、识别文字(OCR)、解读图表/截图。支持本会话已附带的图片(传单个 attachment_id,或传 attachment_ids 数组一次看多张;见消息中的【图片N attachment_id=...】说明),也支持本地图片文件(传 image_path)。"""
    from plugins.dsh_eyes.config import load_config, max_image_bytes
    from plugins.dsh_eyes.eyes import call_vision
    from plugins.dsh_eyes.index import get_index

    config = load_config()
    limit = max_image_bytes(config)
    thread = _tool_thread_id()

    # 多图:attachment_ids 优先(非互斥,对齐 dsh-eyes)
    if attachment_ids:
        ids = [str(aid) for aid in attachment_ids]
        index = get_index()
        items: list[tuple[str, str | None, str | None]] = []
        for number, aid in enumerate(ids, start=1):
            entry = index.get(thread, aid)
            if entry is None:
                items.append((f"图片{number}", None, f"找不到 attachment_id={aid}"))
                continue
            data_url = index.resolve_data_url(thread, aid)
            if data_url is None:
                items.append((f"图片{number}", None, f"读取附件失败:附件字节缺失"))
                continue
            items.append((f"图片{number}", data_url, None))
    elif attachment_id:
        index = get_index()
        entry = index.get(thread, str(attachment_id))
        if entry is None:
            return f"view_image 错误:找不到 attachment_id={attachment_id} 对应的图片。"
        data_url = index.resolve_data_url(thread, str(attachment_id))
        if data_url is None:
            return "view_image 错误:读取附件失败:附件字节缺失"
        items = [("图片", data_url, None)]
    elif image_path:
        context = runtime.context if runtime is not None else None
        workspace = (context or {}).get("workspace") if isinstance(context, dict) else None
        data_url, error = _read_workspace_image(image_path, workspace, limit)
        if error:
            return f"view_image 错误:读取文件失败:{error}"
        items = [("图片", data_url, None)]
    else:
        return "view_image 错误:请提供 attachment_ids、attachment_id 或 image_path。"

    # 全部图片并行调用视觉模型(一次往返),每张错误独立捕获,按输入顺序分段
    async def _describe(label: str, data_url: str | None, error: str | None) -> str:
        if error is not None:
            return f"【{label}】错误:{error}"
        try:
            text = await call_vision(config, data_url, prompt)
            return f"【{label}】{text or '（无内容）'}"
        except RuntimeError as exc:
            return f"【{label}】错误:{exc}"

    results = []
    for label, data_url, error in items:
        results.append(await _describe(label, data_url, error))
    return "\n\n".join(results)


def _tool_thread_id() -> str:
    """从 LangGraph 节点上下文取 thread_id;不在节点上下文时退回 unknown 桶。"""
    try:
        from langgraph.config import get_config

        thread_id = get_config().get("configurable", {}).get("thread_id")
        return str(thread_id) if thread_id else "unknown"
    except RuntimeError:
        return "unknown"


def _resolve_workspace_path(workspace, value: str) -> Path | None:
    """工作区 containment 解析;越界返回 None。workspace 缺失时仅按普通路径解析。"""
    candidate = Path(value).expanduser()
    if workspace:
        root = Path(workspace).resolve()
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if target != root and root not in target.parents:
            return None
        return target
    return candidate.resolve()


def _read_workspace_image(
    image_path: str, workspace: str | None, limit: int
) -> tuple[str | None, str | None]:
    """image_path 分支:containment + 后缀 + 大小上限;返回 (data_url, error)。"""
    path = _resolve_workspace_path(workspace, image_path)
    if path is None:
        return None, f"无法定位文件:{image_path}"
    if path.suffix.lower() not in _IMAGE_SUFFIXES:
        return None, f"不支持的图片类型:{path.suffix}"
    if not path.is_file():
        return None, f"文件不存在:{image_path}"
    size = path.stat().st_size
    if size > limit:
        return None, f"图片 {size} 字节超过上限 {limit} 字节"
    return _to_data_url(path.read_bytes(), path.suffix), None


def _to_data_url(data: bytes, suffix: str) -> str:
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
    }.get(suffix.lower(), "image/png")  # 未知后缀按 png(对齐 dsh-eyes)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
