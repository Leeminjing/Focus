"""本文件对外提供 MaterialUploadService、StoredMaterial 与 inspect_image_file。

输入为任务 ID、FastAPI UploadFile、数据库 session factory 和 ImageResourceLimits；输出为
已提交的 DesktopMaterial 与工作区路径。具体工作流为：分块写入本次请求独占的暂存文件并
实时限制字节数，按实际内容验证图片格式、MIME、解码和像素，独占保留最终文件名后原子提升，
在数据库事务中登记材料；任一阶段失败都会回滚并只清理本次请求拥有的暂存/最终文件。

示例：stored = await MaterialUploadService(factory, limits).store(task_id, upload_file)。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import uuid
import warnings

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.material_files import attachments_dir
from backend.app.desktop.models import DesktopMaterial, DesktopThread, DesktopWorkspace
from backend.app.desktop.resource_limits import ImageResourceLimits
from backend.app.desktop.service_ids import new_desktop_id
from focus.images import IMAGE_MIME_BY_SUFFIX


@dataclass(frozen=True, slots=True)
class ValidatedImageFile:
    format: str
    mime: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class StoredMaterial:
    material: DesktopMaterial
    workspace_path: str


class MaterialUploadService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        limits: ImageResourceLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or ImageResourceLimits()

    async def store(self, task_id: str, upload: UploadFile) -> StoredMaterial:
        filename = Path(upload.filename or "upload.bin").name or "upload.bin"
        workspace_path = await self._workspace_path(task_id)
        directory = attachments_dir(workspace_path)
        temporary = directory / f".upload-{uuid.uuid4().hex}.tmp"
        final: Path | None = None
        try:
            size = await self._receive(upload, temporary, filename)
            image = inspect_image_file(temporary, self._limits.image_pixels)
            self._validate_claims(filename, upload.content_type, size, image)
            final = self._reserve_final(directory, filename)
            os.replace(temporary, final)
            material = await self._register(task_id, workspace_path, final)
            return StoredMaterial(material=material, workspace_path=workspace_path)
        except HTTPException:
            self._cleanup(temporary, final)
            raise
        except Exception as error:
            self._cleanup(temporary, final)
            raise HTTPException(500, f"材料上传失败: {error}") from error
        finally:
            await upload.close()

    async def _workspace_path(self, task_id: str) -> str:
        async with self._session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            workspace = await session.get(DesktopWorkspace, task.workspace_id)
            if workspace is None:
                raise HTTPException(404, "工作区不存在")
            return workspace.path

    async def _receive(self, upload: UploadFile, temporary: Path, filename: str) -> int:
        total = 0
        claimed_image = _claims_image(filename, upload.content_type)
        prefix = bytearray()
        try:
            with temporary.open("xb") as target:
                while True:
                    chunk = await upload.read(self._limits.upload_chunk_bytes)
                    if not chunk:
                        break
                    if len(prefix) < 16:
                        prefix.extend(chunk[: 16 - len(prefix)])
                    claimed_image = claimed_image or _looks_like_supported_image(prefix)
                    limit = (
                        min(self._limits.general_file_bytes, self._limits.original_image_bytes)
                        if claimed_image
                        else self._limits.general_file_bytes
                    )
                    total += len(chunk)
                    if total > limit:
                        raise HTTPException(413, f"文件超过体积上限 {limit} 字节: {filename}")
                    target.write(chunk)
        except FileExistsError as error:
            raise HTTPException(409, "上传暂存路径冲突，请重试") from error
        if total == 0:
            raise HTTPException(422, "上传文件为空")
        return total

    def _validate_claims(
        self,
        filename: str,
        content_type: str | None,
        size: int,
        image: ValidatedImageFile | None,
    ) -> None:
        claimed_image = _claims_image(filename, content_type)
        if image is None:
            if claimed_image:
                raise HTTPException(422, f"图片内容无效或无法解码: {filename}")
            return
        if size > self._limits.original_image_bytes:
            raise HTTPException(
                413, f"图片超过原图体积上限 {self._limits.original_image_bytes} 字节: {filename}"
            )
        suffix_mime = IMAGE_MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
        if suffix_mime != image.mime:
            raise HTTPException(422, f"图片扩展名与实际格式不一致: {filename}")
        if content_type and content_type not in {image.mime, "application/octet-stream"}:
            raise HTTPException(422, f"图片 MIME 与实际格式不一致: {filename}")

    def _reserve_final(self, directory: Path, filename: str) -> Path:
        base = directory / filename
        for candidate in _candidate_paths(base):
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(descriptor)
            return candidate
        raise HTTPException(409, "无法为上传文件分配唯一名称")

    async def _register(
        self, task_id: str, workspace_path: str, final: Path
    ) -> DesktopMaterial:
        material = DesktopMaterial(
            material_id=new_desktop_id(),
            task_id=task_id,
            relative_path=final.relative_to(Path(workspace_path)).as_posix(),
            reading_mode="full",
            instruction_mode="reference",
            retention="removable",
            digest=_digest_file(final),
            git_ref=f"refs/focus/materials/{new_desktop_id()}",
        )
        async with self._session_factory() as session:
            session.add(material)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return material

    @staticmethod
    def _cleanup(temporary: Path, final: Path | None) -> None:
        for path in (temporary, final):
            if path is not None:
                path.unlink(missing_ok=True)


def inspect_image_file(path: Path, max_pixels: int) -> ValidatedImageFile | None:
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                width, height = int(image.width), int(image.height)
                mime = str(Image.MIME.get(image_format) or "")
                if mime not in set(IMAGE_MIME_BY_SUFFIX.values()):
                    raise HTTPException(422, f"不支持的图片格式: {image_format or 'unknown'}")
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise HTTPException(413, f"图片像素超过上限 {max_pixels}: {width}x{height}")
                image.verify()
                return ValidatedImageFile(image_format, mime, width, height)
    except UnidentifiedImageError:
        return None
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(422, f"图片内容损坏或无法解码: {path.name}") from error


def _claims_image(filename: str, content_type: str | None) -> bool:
    return Path(filename).suffix.lower() in IMAGE_MIME_BY_SUFFIX or bool(
        content_type and content_type.startswith("image/")
    )


def _looks_like_supported_image(prefix: bytes | bytearray) -> bool:
    data = bytes(prefix)
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a", b"BM"))
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _candidate_paths(base: Path):
    yield base
    for _ in range(100):
        yield base.with_name(f"{base.stem}-{uuid.uuid4().hex[:8]}{base.suffix}")


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
