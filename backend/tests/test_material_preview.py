"""backend/tests/test_material_preview.py

本文件验证材料文本预览的读取与解码边界。输入为磁盘上的材料文件（不同编码、不同体积）与读取上限，
输出为 MaterialText 的正文、编码名、截断标记与真实体积断言；工作流不访问数据库、不启动 Gateway，
仅以 tmp_path 造文件后直接调用纯读取函数。
"""

import pytest

from backend.app.desktop.material_files import (
    PREVIEW_TEXT_MAX_BYTES,
    TEXT_TRUNCATION_MARKER,
    read_material_text,
)


def test_decodes_plain_utf8(tmp_path):
    path = tmp_path / "note.md"
    # 用 write_bytes 控制确切字节，避免 Windows 上 write_text 静默把 \n 变成 \r\n
    path.write_bytes("# 标题\n正文内容\n".encode("utf-8"))

    result = read_material_text(path)

    assert result.text == "# 标题\n正文内容\n"
    assert result.encoding == "utf-8-sig"
    assert result.truncated is False
    assert result.size_bytes == path.stat().st_size


def test_strips_utf8_bom(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes("第一行\n第二行\n".encode("utf-8-sig"))

    result = read_material_text(path)

    assert not result.text.startswith("\ufeff")
    assert result.text.splitlines()[0] == "第一行"
    assert result.truncated is False


def test_decodes_gb18030_chinese(tmp_path):
    body = "中文内容：这是一段用 GB18030 保存的说明。\n第二行。\n"
    path = tmp_path / "gbk.txt"
    path.write_bytes(body.encode("gb18030"))

    result = read_material_text(path)

    assert result.text == body
    assert result.encoding == "gb18030"
    assert "\ufffd" not in result.text


def test_decodes_gbk_subset(tmp_path):
    body = "国标码文本\n"
    path = tmp_path / "gbk-only.txt"
    path.write_bytes(body.encode("gbk"))

    result = read_material_text(path)

    assert result.text == body


def test_truncates_beyond_limit_and_marks_it(tmp_path):
    path = tmp_path / "big.log"
    path.write_bytes(b"a" * 500)

    result = read_material_text(path, max_bytes=100)

    assert result.truncated is True
    assert result.size_bytes == 500
    assert result.text.startswith("a" * 100)
    assert result.text.endswith(TEXT_TRUNCATION_MARKER)


def test_truncation_at_exact_limit_is_not_truncated(tmp_path):
    path = tmp_path / "exact.txt"
    path.write_bytes(b"b" * 100)

    result = read_material_text(path, max_bytes=100)

    assert result.truncated is False
    assert result.text == "b" * 100


def test_truncation_does_not_split_multibyte_character(tmp_path):
    # 每个汉字占 3 字节，上限刻意落在第 4 个字的中间
    path = tmp_path / "cn.txt"
    path.write_bytes("中文内容测试".encode("utf-8"))

    result = read_material_text(path, max_bytes=11)

    assert result.truncated is True
    assert "\ufffd" not in result.text
    assert result.text.startswith("中文内")
    assert result.text.endswith(TEXT_TRUNCATION_MARKER)


def test_empty_file_is_readable_text(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    result = read_material_text(path)

    assert result.text == ""
    assert result.truncated is False
    assert result.size_bytes == 0


def test_undecodable_content_raises_unicode_error(tmp_path):
    # 单独一个未配对的 GB18030 前导字节，在任何已知编码下都不成立
    path = tmp_path / "broken.bin"
    path.write_bytes(b"\x81")

    with pytest.raises(UnicodeDecodeError):
        read_material_text(path)


def test_default_limit_is_used_when_omitted(tmp_path):
    path = tmp_path / "under-limit.txt"
    path.write_bytes(b"c" * (PREVIEW_TEXT_MAX_BYTES - 1))

    result = read_material_text(path)

    assert result.truncated is False
    assert len(result.text) == PREVIEW_TEXT_MAX_BYTES - 1
