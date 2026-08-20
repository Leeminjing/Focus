"""dsh-eyes 视觉请求:data URL 内联的 OpenAI 兼容调用(chat 与 responses 双协议)。

对外提供:
    describe_image(config, data_url, prompt=None) -> str — 单图描述
    describe_images(config, data_urls, prompt=None) -> str — 多图并行、独立错误、【图片N】分段

请求语义(对齐 dsh-eyes lib/index.js):
    - 瞬态失败自动重试:429/5xx/网络错误最多 3 次尝试、指数退避 400×2ⁿ ms;
      非 429 的 4xx 与 API 层错误立即失败
    - 协议:style=chat → messages(text + image_url);style=responses → input(input_text + input_image),
      响应从 output 解析 output_text
    - 取消:请求期间的取消(CancelledError)立即停止,不重试
    - 默认 prompt:「请详细描述这张图片的内容,并识别图中所有文字(OCR)。」
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = "请详细描述这张图片的内容，并识别图中所有文字（OCR）。"

_MAX_ATTEMPTS = 3


def _build_body(config: dict, data_url: str, prompt: str, style: str) -> dict:
    if style == "responses":
        return {
            "model": config["vision_model"],
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
        }
    return {
        "model": config["vision_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }


def _extract_text(data: dict, style: str) -> str:
    if style == "responses":
        parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return content if isinstance(content, str) else ""


def _is_api_error(status: int, body: dict) -> bool:
    """响应 2xx 但 body 带 error 字段 → API 层错误(立即失败,不重试)。"""
    return isinstance(body.get("error"), (dict, str))


async def _post_once(config: dict, data_url: str, prompt: str, style: str):
    """单次请求;返回 (ok, text, status, error_body)。"""
    import httpx

    from plugins.dsh_eyes.config import endpoint_for_style, resolve_api_key

    url = endpoint_for_style(config, style)
    key = resolve_api_key(config)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                url,
                json=_build_body(config, data_url, prompt, style),
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}", 0, {}
    if response.status_code != 200:
        text = response.text[:500] if hasattr(response, "text") else ""
        return False, f"HTTP {response.status_code}: {text}", response.status_code, {}
    try:
        body = response.json()
    except ValueError as exc:
        return False, f"响应解析失败: {exc}", 200, {}
    if _is_api_error(response.status_code, body):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message") or str(error)
        return False, f"vision API error: {error}", response.status_code, body
    return True, _extract_text(body, style), response.status_code, body


async def call_vision(config: dict, data_url: str, prompt: str | None = None) -> str:
    """单图视觉调用(对齐 dsh-eyes callVision):瞬态重试,失败抛 RuntimeError。

    重试:429/5xx/网络错误 → 3 次尝试、指数退避 400×2ⁿ ms;
    非 429 的 4xx 与 API 层错误立即抛;取消立即抛(不重试)。
    """
    from plugins.dsh_eyes.config import resolve_style

    style = resolve_style(config)
    prompt = (prompt or "").strip() or DEFAULT_PROMPT
    last_error = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            ok, text, status, _body = await _post_once(config, data_url, prompt, style)
        except asyncio.CancelledError:
            raise  # 取消立即停止,不重试
        if ok:
            return text
        retryable = status == 429 or (500 <= status <= 599) or status == 0  # 0 = 网络错误
        last_error = text
        if not retryable:
            raise RuntimeError(last_error)
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(0.4 * (2 ** attempt))
    raise RuntimeError(last_error)


async def describe_image(config: dict, data_url: str, prompt: str | None = None) -> str:
    """单图描述(不抛版,供 service.vision / spatial-patrol 消费);失败返回错误文本。"""
    try:
        text = await call_vision(config, data_url, prompt)
        return text or "（无内容）"
    except RuntimeError as exc:
        return f"【错误】{exc}"
