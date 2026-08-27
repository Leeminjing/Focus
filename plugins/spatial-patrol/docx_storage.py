"""Lossless DOCX validation, conflict detection and atomic replacement."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx


MAX_DOCX_BYTES = 256 * 1024 * 1024
REQUIRED_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "word/document.xml"})
_path_locks: dict[str, asyncio.Lock] = {}
_path_locks_guard = asyncio.Lock()


class DocxSaveError(RuntimeError):
    pass


class DocxConflictError(DocxSaveError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as package:
            names = frozenset(package.namelist())
            missing = REQUIRED_PARTS - names
            if missing:
                raise DocxSaveError(f"DOCX 缺少关键部件: {', '.join(sorted(missing))}")
            bad_member = package.testzip()
            if bad_member:
                raise DocxSaveError(f"DOCX ZIP 校验失败: {bad_member}")
            for name in names:
                if name.endswith((".xml", ".rels")):
                    ElementTree.fromstring(package.read(name))
    except DocxSaveError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocxSaveError(f"无效 DOCX: {exc}") from exc


def normalized_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DocxSaveError("Document Server 返回了非法下载 URL")
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{parsed.port or default_port}"


def validate_download_url(url: str, allowed_origins: set[str]) -> None:
    allowed = {normalized_origin(origin) for origin in allowed_origins}
    if normalized_origin(url) not in allowed:
        raise DocxSaveError("Document Server 下载 URL origin 不受信任")


async def _path_lock(path: Path) -> asyncio.Lock:
    key = os.path.normcase(str(path.resolve()))
    async with _path_locks_guard:
        return _path_locks.setdefault(key, asyncio.Lock())


async def download_docx(
    url: str,
    destination: Path,
    *,
    allowed_origins: set[str],
    max_bytes: int = MAX_DOCX_BYTES,
    client: httpx.AsyncClient | None = None,
) -> None:
    validate_download_url(url, allowed_origins)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
    try:
        async with client.stream("GET", url) as response:
            if 300 <= response.status_code < 400:
                redirect = urljoin(url, response.headers.get("location", ""))
                validate_download_url(redirect, allowed_origins)
                raise DocxSaveError("Document Server 下载重定向被拒绝")
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise DocxSaveError("DOCX 保存响应超过大小限制")
            received = 0
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise DocxSaveError("DOCX 保存响应超过大小限制")
                    output.write(chunk)
    except httpx.HTTPError as exc:
        raise DocxSaveError(f"下载 Document Server 保存结果失败: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()


async def atomic_save_from_url(
    target: Path,
    url: str,
    *,
    expected_hash: str,
    allowed_origins: set[str],
    client: httpx.AsyncClient | None = None,
) -> str:
    """Validate into a same-directory temp file then atomically replace target."""
    lock = await _path_lock(target)
    async with lock:
        if not target.is_file():
            raise DocxConflictError("原 DOCX 已不存在")
        current_hash = await asyncio.to_thread(file_sha256, target)
        if current_hash != expected_hash:
            raise DocxConflictError("DOCX 已被会话外程序修改，拒绝覆盖")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".saving", dir=target.parent
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            await download_docx(
                url, temp_path, allowed_origins=allowed_origins, client=client
            )
            await asyncio.to_thread(validate_docx, temp_path)
            saved_hash = await asyncio.to_thread(file_sha256, temp_path)
            os.replace(temp_path, target)
            return saved_hash
        except PermissionError as exc:
            raise DocxConflictError("DOCX 正被 Word/WPS 占用，未覆盖原文件") from exc
        finally:
            temp_path.unlink(missing_ok=True)
