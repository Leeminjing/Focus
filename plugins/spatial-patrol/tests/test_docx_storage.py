import asyncio
import zipfile
from pathlib import Path

import httpx

from plugins.spatial_patrol.docx_storage import (
    DocxConflictError,
    DocxSaveError,
    atomic_save_from_url,
    file_sha256,
    validate_docx,
    download_docx,
)


def _docx_bytes(text: str, extra: bytes = b"untouched") -> bytes:
    import io

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("[Content_Types].xml", "<Types xmlns='urn:test'/>")
        package.writestr("_rels/.rels", "<Relationships xmlns='urn:test'/>")
        package.writestr("word/document.xml", f"<document xmlns='urn:test'><p>{text}</p></document>")
        package.writestr("customXml/item1.bin", extra)
    return output.getvalue()


def test_atomic_save_validates_package_and_preserves_unmodified_parts(tmp_path):
    target = tmp_path / "rich.docx"
    target.write_bytes(_docx_bytes("before"))
    expected_hash = file_sha256(target)
    replacement = _docx_bytes("after")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=replacement))

    async def scenario():
        async with httpx.AsyncClient(transport=transport) as client:
            return await atomic_save_from_url(
                target, "http://127.0.0.1:18080/cache/result.docx",
                expected_hash=expected_hash,
                allowed_origins={"http://127.0.0.1:18080"}, client=client,
            )

    saved_hash = asyncio.run(scenario())
    assert saved_hash == file_sha256(target)
    with zipfile.ZipFile(target) as package:
        assert b"after" in package.read("word/document.xml")
        assert package.read("customXml/item1.bin") == b"untouched"


def test_corrupt_or_conflicting_save_never_replaces_original(tmp_path):
    target = tmp_path / "rich.docx"
    original = _docx_bytes("before")
    target.write_bytes(original)

    async def corrupt():
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not-a-docx"))
        async with httpx.AsyncClient(transport=transport) as client:
            await atomic_save_from_url(
                target, "http://127.0.0.1:18080/cache/result.docx",
                expected_hash=file_sha256(target),
                allowed_origins={"http://127.0.0.1:18080"}, client=client,
            )

    try:
        asyncio.run(corrupt())
        raise AssertionError("corrupt save should fail")
    except DocxSaveError:
        pass
    assert target.read_bytes() == original

    try:
        asyncio.run(atomic_save_from_url(
            target, "http://127.0.0.1:18080/cache/result.docx",
            expected_hash="0" * 64,
            allowed_origins={"http://127.0.0.1:18080"},
        ))
        raise AssertionError("hash conflict should fail")
    except DocxConflictError:
        pass
    assert target.read_bytes() == original


def test_validate_docx_requires_opc_parts(tmp_path):
    path = tmp_path / "bad.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", "<document/>")
    try:
        validate_docx(path)
        raise AssertionError("missing OPC parts should fail")
    except DocxSaveError:
        pass


def test_download_rejects_external_origin_redirect_and_oversize(tmp_path):
    destination = tmp_path / "download.docx"
    try:
        asyncio.run(download_docx(
            "https://attacker.example/file.docx", destination,
            allowed_origins={"http://127.0.0.1:18080"},
        ))
        raise AssertionError("external origin should fail")
    except DocxSaveError:
        pass

    async def redirect():
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            302, headers={"location": "https://attacker.example/file.docx"}
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            await download_docx(
                "http://127.0.0.1:18080/file.docx", destination,
                allowed_origins={"http://127.0.0.1:18080"}, client=client,
            )

    try:
        asyncio.run(redirect())
        raise AssertionError("redirect should fail")
    except DocxSaveError:
        pass

    async def oversize():
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"content-length": "1000"}, content=b"x"
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            await download_docx(
                "http://127.0.0.1:18080/file.docx", destination,
                allowed_origins={"http://127.0.0.1:18080"}, max_bytes=10, client=client,
            )

    try:
        asyncio.run(oversize())
        raise AssertionError("oversize should fail")
    except DocxSaveError:
        pass
