"""本文件验证有界上传的资源、内容和补偿边界。

输入为内存 UploadFile、工作区内测试目录与可注入 ImageResourceLimits；输出为分块超限、真实
图片识别、格式冲突、同名不覆盖、成功提升和登记失败清理断言。测试用子类只替换数据库登记，
文件生命周期仍执行真实 MaterialUploadService 工作流。

示例：python -m pytest backend/tests/test_material_upload_lifecycle.py。
"""

import asyncio
import io
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

from fastapi import HTTPException, UploadFile
import pytest
from starlette.datastructures import Headers

from backend.app.desktop.material_upload import MaterialUploadService, inspect_image_file
from backend.app.desktop.resource_limits import ImageResourceLimits


@pytest.fixture
def workspace():
    root = Path(__file__).resolve().parents[2] / "tmp" / f"upload-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def png(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def upload(name: str, data: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


class RecordingUploadFile(UploadFile):
    def __init__(self, name: str, data: bytes, content_type: str) -> None:
        super().__init__(
            file=io.BytesIO(data),
            filename=name,
            headers=Headers({"content-type": content_type}),
        )
        self.read_sizes = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return await super().read(size)


class UploadService(MaterialUploadService):
    def __init__(self, workspace: Path, limits=None, fail_registration=False):
        super().__init__(None, limits)
        self.workspace = workspace
        self.fail_registration = fail_registration

    async def _workspace_path(self, task_id: str) -> str:
        return str(self.workspace)

    async def _register(self, task_id: str, workspace_path: str, final: Path):
        if self.fail_registration:
            raise RuntimeError("injected registration failure")
        return SimpleNamespace(material_id="m1", relative_path=final.relative_to(self.workspace).as_posix())


class FailingSession:
    def __init__(self):
        self.rolled_back = False
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        raise RuntimeError("injected database flush/commit failure")

    async def rollback(self):
        self.rolled_back = True


class DatabaseFailureUpload(MaterialUploadService):
    def __init__(self, workspace: Path, session: FailingSession):
        super().__init__(lambda: session)
        self.workspace = workspace

    async def _workspace_path(self, task_id: str) -> str:
        return str(self.workspace)


def test_valid_image_is_promoted_and_same_name_never_overwrites(workspace) -> None:
    service = UploadService(workspace)
    first = asyncio.run(service.store("task", upload("shot.png", png(), "image/png")))
    second = asyncio.run(service.store("task", upload("shot.png", png(9, 9), "image/png")))
    assert first.material.relative_path != second.material.relative_path
    files = sorted((workspace / ".focus" / "attachments").glob("*.png"))
    assert len(files) == 2
    assert files[0].read_bytes() != b""


def test_same_name_concurrent_uploads_reserve_distinct_files(workspace) -> None:
    service = UploadService(workspace)

    async def run_uploads():
        return await asyncio.gather(*[
            service.store("task", upload("shot.png", png(8 + index, 8), "image/png"))
            for index in range(6)
        ])

    stored = asyncio.run(run_uploads())
    paths = [item.material.relative_path for item in stored]
    assert len(set(paths)) == 6
    assert len(list((workspace / ".focus" / "attachments").glob("*.png"))) == 6


def test_fake_image_and_mime_extension_conflict_are_rejected_without_files(workspace) -> None:
    service = UploadService(workspace)
    with pytest.raises(HTTPException) as fake:
        asyncio.run(service.store("task", upload("fake.png", b"not-image", "image/png")))
    assert fake.value.status_code == 422
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(service.store("task", upload("photo.jpg", png(), "image/jpeg")))
    assert mismatch.value.status_code == 422
    with pytest.raises(HTTPException) as wrong_mime:
        asyncio.run(service.store("task", upload("photo.png", png(), "text/plain")))
    assert wrong_mime.value.status_code == 422
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_corrupt_and_unsupported_images_are_rejected(workspace) -> None:
    from PIL import Image

    service = UploadService(workspace)
    damaged = b"\x89PNG\r\n\x1a\n" + b"broken"
    with pytest.raises(HTTPException) as corrupt:
        asyncio.run(service.store("task", upload("bad.png", damaged, "image/png")))
    assert corrupt.value.status_code == 422
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="TIFF")
    with pytest.raises(HTTPException) as unsupported:
        asyncio.run(service.store("task", upload("scan.tiff", buffer.getvalue(), "image/tiff")))
    assert unsupported.value.status_code == 422
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_general_limit_stops_stream_and_cleans_temporary_file(workspace) -> None:
    limits = ImageResourceLimits(general_file_bytes=5, original_image_bytes=4, upload_chunk_bytes=2)
    service = UploadService(workspace, limits)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.store("task", upload("data.bin", b"123456", "application/octet-stream")))
    assert error.value.status_code == 413
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_upload_reads_only_configured_chunks(workspace) -> None:
    limits = ImageResourceLimits(upload_chunk_bytes=3)
    service = UploadService(workspace, limits)
    source = RecordingUploadFile("notes.txt", b"1234567", "text/plain")
    asyncio.run(service.store("task", source))
    assert source.read_sizes == [3, 3, 3, 3]


def test_actual_image_signature_applies_image_limit_during_receive(workspace) -> None:
    limits = ImageResourceLimits(
        general_file_bytes=1024,
        original_image_bytes=5,
        upload_chunk_bytes=2,
    )
    service = UploadService(workspace, limits)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            service.store(
                "task",
                upload("disguised.bin", png(), "application/octet-stream"),
            )
        )
    assert error.value.status_code == 413
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_registration_failure_compensates_final_and_temporary_files(workspace) -> None:
    service = UploadService(workspace, fail_registration=True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.store("task", upload("shot.png", png(), "image/png")))
    assert error.value.status_code == 500
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_database_flush_or_commit_failure_rolls_back_and_removes_final(workspace) -> None:
    session = FailingSession()
    service = DatabaseFailureUpload(workspace, session)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.store("task", upload("shot.png", png(), "image/png")))
    assert error.value.status_code == 500
    assert session.added
    assert session.rolled_back is True
    assert not list((workspace / ".focus" / "attachments").iterdir())


def test_receive_and_promotion_failures_clean_only_owned_files(workspace, monkeypatch) -> None:
    class ReceiveFailure(UploadService):
        async def _receive(self, upload_file, temporary, filename):
            temporary.write_bytes(b"partial")
            raise RuntimeError("injected receive failure")

    with pytest.raises(HTTPException):
        asyncio.run(ReceiveFailure(workspace).store("task", upload("x.txt", b"x", "text/plain")))
    directory = workspace / ".focus" / "attachments"
    assert not list(directory.iterdir())

    monkeypatch.setattr(os, "replace", lambda _source, _target: (_ for _ in ()).throw(OSError("move")))
    with pytest.raises(HTTPException):
        asyncio.run(UploadService(workspace).store("task", upload("x.txt", b"x", "text/plain")))
    assert not list(directory.iterdir())


def test_pixel_limit_and_configuration_validation(workspace) -> None:
    path = workspace / "large.png"
    path.write_bytes(png(10, 10))
    with pytest.raises(HTTPException) as error:
        inspect_image_file(path, 99)
    assert error.value.status_code == 413
    with pytest.raises(ValueError):
        ImageResourceLimits(upload_chunk_bytes=0)
    with pytest.raises(ValueError):
        ImageResourceLimits(general_file_bytes=10, original_image_bytes=11)
    with pytest.raises(ValueError):
        ImageResourceLimits(general_file_bytes=10, original_image_bytes=9, upload_chunk_bytes=11)
    defaults = ImageResourceLimits()
    assert defaults.general_file_bytes == 100 * 1024 * 1024
    assert defaults.original_image_bytes == 20 * 1024 * 1024
    assert defaults.image_pixels == 40_000_000
    assert defaults.run_image_count == 16
    assert defaults.run_model_bytes == 32 * 1024 * 1024
