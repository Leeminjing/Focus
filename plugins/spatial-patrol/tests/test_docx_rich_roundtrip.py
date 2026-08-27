import asyncio
import zipfile

import httpx

from plugins.spatial_patrol.docx_storage import atomic_save_from_url, file_sha256, validate_docx
from plugins.spatial_patrol.tests.complex_docx_fixture import (
    build_complex_docx,
    mutate_main_document,
)


def test_rich_fixture_noop_is_byte_exact_and_edit_preserves_unrelated_parts(tmp_path):
    target = tmp_path / "complex-rich.docx"
    build_complex_docx(target)
    validate_docx(target)
    original = target.read_bytes()
    before_hash = file_sha256(target)
    assert target.read_bytes() == original  # opening/closing without callback is a byte no-op
    with zipfile.ZipFile(target) as package:
        original_names = set(package.namelist())
        original_extension = package.read("customXml/focus-extension.bin")
        assert "word/header1.xml" in original_names
        assert "word/footer1.xml" in original_names
        assert any(name.startswith("word/media/") for name in original_names)
        assert b"FocusWatermark" in package.read("word/header1.xml")

    replacement = mutate_main_document(target)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=replacement))

    async def save():
        async with httpx.AsyncClient(transport=transport) as client:
            return await atomic_save_from_url(
                target, "http://127.0.0.1:18080/cache/complex-rich.docx",
                expected_hash=before_hash,
                allowed_origins={"http://127.0.0.1:18080"}, client=client,
            )

    asyncio.run(save())
    with zipfile.ZipFile(target) as package:
        assert set(package.namelist()) == original_names
        assert package.read("customXml/focus-extension.bin") == original_extension
        assert b"Edited first page body" in package.read("word/document.xml")
