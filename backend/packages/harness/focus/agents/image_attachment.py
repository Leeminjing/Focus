"""本文件对外提供图片附件请求投影中间件与图片交付失败类型。

输入为 runtime context 中的 RunImageInputs、工作区路径和模型图像能力；输出为只存在于
当前模型请求中的 human 图像内容块。具体工作流为：每次模型调用读取 attached 清单，先校验
模型能力，再从未变化路径或受保护 Git blob 读取原图、生成送模副本并追加到 request.messages；
任何附件不可读
都在调用模型前失败，像素不写入 graph state 或 checkpoint。

示例：middlewares.append(build_image_attachment_middleware())。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from focus.agents.image_inputs import MODEL_IMAGE_INPUT_KEY, RunImageInput, RunImageInputs
from focus.images import image_dimensions, scale_for_model, to_data_url

_INJECTION_PREAMBLE = "以下是用户本轮附加的图片材料："


class ImageMaterialUnavailable(RuntimeError):
    def __init__(self, material_id: str, relative_path: str, reason: str) -> None:
        self.material_id = material_id
        self.relative_path = relative_path
        super().__init__(
            f"本轮附加的图片材料不可读: {relative_path} (material_id={material_id})，{reason}"
        )


class ImageModelCannotReadImages(RuntimeError):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(
            f"本轮存在图片附件，但模型 '{model_name}' 未声明具备图像输入能力；"
            "请声明 supports_image_input 或改用具备图像输入的模型"
        )


class ImageAttachmentProjectionMiddleware(AgentMiddleware):
    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._project(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._project(request))

    def _project(self, request: Any) -> Any:
        context = _runtime_context(getattr(request, "runtime", None))
        inputs = RunImageInputs.from_context(context)
        if not inputs.attached:
            return request
        _require_image_capable_model(context)
        workspace = _workspace_of(context)
        content: list[dict[str, Any]] = [{"type": "text", "text": _INJECTION_PREAMBLE}]
        content.extend(_image_block(workspace, item) for item in inputs.attached)
        return request.override(messages=[*request.messages, HumanMessage(content=content)])


def build_image_attachment_middleware() -> ImageAttachmentProjectionMiddleware:
    return ImageAttachmentProjectionMiddleware()


def _runtime_context(runtime: Any) -> Any:
    return getattr(runtime, "context", None)


def _require_image_capable_model(context: Any) -> None:
    if isinstance(context, dict) and context.get(MODEL_IMAGE_INPUT_KEY) is True:
        return
    model_name = context.get("model_name") if isinstance(context, dict) else None
    raise ImageModelCannotReadImages(str(model_name or "未知模型"))


def _workspace_of(context: Any) -> Path:
    workspace = context.get("workspace") if isinstance(context, dict) else None
    if not workspace:
        raise ImageMaterialUnavailable("-", "-", "缺少工作区上下文")
    return Path(str(workspace)).resolve()


def _image_block(workspace: Path, material: RunImageInput) -> dict[str, Any]:
    path = _material_path(workspace, material)
    try:
        original = _source_bytes(workspace, path, material)
        expected = material.source_bytes or len(original)
    except OSError as error:
        raise ImageMaterialUnavailable(
            material.material_id, material.relative_path, str(error)
        ) from error
    if not original or len(original) != expected:
        raise ImageMaterialUnavailable(
            material.material_id, material.relative_path, "材料为空或在运行期间发生变化"
        )
    if material.digest and material.digest != "legacy" and hashlib.sha256(original).hexdigest() != material.digest:
        raise ImageMaterialUnavailable(
            material.material_id, material.relative_path, "材料摘要与发送时快照不一致"
        )
    if image_dimensions(original) is None:
        raise ImageMaterialUnavailable(
            material.material_id, material.relative_path, "材料已不再是有效图片"
        )
    mime, scaled = scale_for_model(original)
    return {
        "type": "image_url",
        "material_id": material.material_id,
        "image_url": {"url": to_data_url(mime, scaled)},
    }


def _source_bytes(workspace: Path, path: Path, material: RunImageInput) -> bytes:
    if material.snapshot_object_id:
        if not re.fullmatch(r"[0-9a-f]{40,64}", material.snapshot_object_id):
            raise OSError("受保护图片快照标识无效")
        process = subprocess.run(
            ["git", "-C", str(workspace), "cat-file", "blob", material.snapshot_object_id],
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise OSError(process.stderr.decode("utf-8", errors="replace") or "受保护图片快照不可读")
        return process.stdout
    expected = material.source_bytes or path.stat().st_size
    with path.open("rb") as source:
        return source.read(expected + 1)


def _material_path(workspace: Path, material: RunImageInput) -> Path:
    root = workspace.resolve()
    path = root.joinpath(*Path(material.relative_path).parts).resolve()
    if not path.is_relative_to(root):
        raise ImageMaterialUnavailable(
            material.material_id, material.relative_path, "材料路径超出工作区"
        )
    return path
