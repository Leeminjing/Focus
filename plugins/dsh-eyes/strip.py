"""dsh-eyes 图片剥离 hook(before_model):image 块 → 【图片N attachment_id=…】 文本引用。

工作流(对齐 dsh-eyes lib/index.js 的 admission + stripping):
    (1) 透传判定(还原 shouldProtect 语义):
        - 主模型在 passthrough_models 名单中 → 不剥离
        - 主模型非 DeepSeek 前缀(视为原生多模态)→ 不剥离
        - 主模型未知或 text-only → 剥离(保守默认,对齐 dsh-eyes「unknown → protect」)
    (2) 递归扫描 content 块(含嵌套 content 列表),对每个 image_url 块:
        - 以 sha1(url) 前 12 位为 attachment_id 登记进当前 thread 的附件索引
          (同图重复出现命中既有 id)
        - 原位替换为「【图片N attachment_id=…】查看请调 view_image(attachment_id=…)」
        - URL 缺失/非法 → 「【图片N】附带一张图片,但无法定位其附件 id。」
    (3) 无 image 块 → 返回 None(不修改 state)
"""

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_main_model_name: Any = None  # 模块级缓存:首次读 AppConfig.models[0].model


def _attachment_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _thread_id(runtime: Any) -> str:
    execution = getattr(runtime, "execution_info", None)
    thread_id = getattr(execution, "thread_id", None)
    return str(thread_id) if thread_id else "unknown"


def _system_model_name() -> str | None:
    """系统主模型名(模块级缓存);配置不可得时返回 None(→ 保守剥离)。"""
    global _main_model_name
    if _main_model_name is None:
        try:
            from focus.config import get_app_config

            models = get_app_config("config.yaml").models
            _main_model_name = str(models[0].model or "") if models else ""
        except Exception:
            _main_model_name = ""
    return _main_model_name or None


def _should_protect(config: dict) -> bool:
    """还原 dsh-eyes shouldProtect:名单透传 / 多模态不保护 / 未知与 text-only 保护。"""
    model_name = _system_model_name()
    if model_name is None:
        return True  # 未知 → 保守保护
    if model_name in {str(name) for name in (config.get("passthrough_models") or [])}:
        return False
    if not model_name.startswith("deepseek"):
        return False  # 非 DeepSeek 视为原生多模态,透传
    return True


def _strip_blocks(
    blocks: list, thread: str, counter: dict[str, int], pending: list[tuple[str, str, str]]
) -> list:
    """递归替换 image_url 块(含嵌套 content 列表);返回新块列表,登记进 pending。"""
    out: list = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "image_url":
            url = (block.get("image_url") or {}).get("url")
            counter["n"] += 1
            if isinstance(url, str) and url:
                attachment_id = _attachment_id(url)
                pending.append((thread, attachment_id, url))
                out.append({
                    "type": "text",
                    "text": (
                        f"【图片{counter['n']} attachment_id={attachment_id}】"
                        f"查看请调 view_image(attachment_id={attachment_id})"
                    ),
                })
            else:
                out.append({
                    "type": "text",
                    "text": f"【图片{counter['n']}】附带一张图片，但无法定位其附件 id。",
                })
        elif isinstance(block, dict) and isinstance(block.get("content"), list):
            out.append({
                **block,
                "content": _strip_blocks(block["content"], thread, counter, pending),
            })
        else:
            out.append(block)
    return out


async def strip_images(state: dict, runtime: Any) -> dict | None:
    """把消息中的 image 块剥离为文本引用,图片登记进会话索引。"""
    from plugins.dsh_eyes.config import load_config
    from plugins.dsh_eyes.index import get_index

    config = load_config()
    if not _should_protect(config):
        return None

    messages = state.get("messages", [])
    index = get_index()
    thread = _thread_id(runtime)
    counter: dict[str, int] = {"n": 0}
    pending: list[tuple[str, str, str]] = []
    changed = False
    rebuilt: list = []

    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            rebuilt.append(message)
            continue
        new_content = _strip_blocks(content, thread, counter, pending)
        if new_content != content:
            rebuilt.append(message.model_copy(update={"content": new_content}))
            changed = True
        else:
            rebuilt.append(message)

    if not changed:
        return None
    from plugins.dsh_eyes.config import attachment_max_bytes

    limit = attachment_max_bytes(config)
    for attachment_thread, attachment_id, url in pending:
        try:
            await index.attach_bytes(attachment_thread, attachment_id, url, limit)
        except ValueError:
            logger.warning(
                "dsh-eyes: 附件落盘失败 attachment_id=%s,已跳过", attachment_id,
                exc_info=True,
            )
    return {"messages": rebuilt}
