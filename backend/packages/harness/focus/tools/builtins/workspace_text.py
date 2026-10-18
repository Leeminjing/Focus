"""有界 UTF-8 行读取与精确替换；保留原始换行/BOM，失败时不交付写入。

输入为已准入的真实路径、行游标或旧/新文本；输出为带版本摘要的页或实际 diff。
工具层负责权限和版本观察，本模块负责内容匹配、字节保真和原子文件替换。
"""

from __future__ import annotations

import difflib
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile

from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, StrictStr

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_CHARS = 20000


class TextEdit(BaseModel):
    """一次必须在原文件中唯一匹配的精确替换，不做空白或换行归一化。"""

    model_config = ConfigDict(extra="forbid")
    old_text: StrictStr
    new_text: StrictStr


def read_utf8(target: Path) -> tuple[bytes, str]:
    """限制单文件输入，并拒绝会破坏精确匹配的二进制/解码替换。"""
    try:
        if not target.is_file():
            raise ToolException(f"目标不是现有文件: {target}")
        with target.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ToolException(f"文件超过 {MAX_FILE_BYTES} 字节上限，请缩小文件或使用专用工具: {target}")
        if b"\0" in raw:
            raise ToolException(f"二进制文件不能按 UTF-8 文本处理: {target}")
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolException(f"文件不是有效 UTF-8，未修改: {target}") from error
    except OSError as error:
        raise ToolException(f"无法读取文件 {target}: {error}") from error


def check_version(raw: bytes, expected_sha256: str | None) -> str:
    digest = sha256(raw).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ToolException("文件版本不匹配，未修改；请重新读取后重试")
    return digest


def text_lines(text: str) -> list[tuple[str, str]]:
    """只把 CRLF/LF/CR 当作物理行；保留每行终止符供精确的多行编辑使用。"""
    parts = re.split(r"(\r\n|\r|\n)", text)
    lines = [(parts[index], parts[index + 1]) for index in range(0, len(parts) - 1, 2)]
    if parts[-1]:
        lines.append((parts[-1], ""))
    return lines


def read_lines(
    target: Path, start_line: int, max_lines: int, start_column: int,
    expected_sha256: str | None,
) -> dict:
    if start_line < 1 or not 1 <= max_lines <= 500 or start_column < 1:
        raise ToolException("start_line/start_column 必须 >= 1，max_lines 必须在 1..500")
    raw, text = read_utf8(target)
    digest = check_version(raw, expected_sha256)
    lines = text_lines(text.removeprefix("\ufeff"))
    if start_line > len(lines) + 1:
        raise ToolException(f"start_line 超出文件范围，共 {len(lines)} 行")
    page: list[dict] = []
    remaining = MAX_OUTPUT_CHARS
    next_line = next_column = None
    for index in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
        column = start_column - 1 if index == start_line - 1 else 0
        line, ending = lines[index]
        if column > len(line):
            raise ToolException("start_column 超出该行范围")
        fragment = line[column:column + remaining]
        complete = column + len(fragment) == len(line)
        page.append({"line": index + 1, "column": column + 1, "text": fragment,
                     "line_ending": ending if complete else ""})
        remaining -= len(fragment)
        if column + len(fragment) < len(line):
            next_line, next_column = index + 1, column + len(fragment) + 1
            break
        if index + 1 < len(lines):
            next_line, next_column = index + 2, 1
        else:
            next_line = next_column = None
        if remaining == 0:
            break
    return {"path": str(target), "sha256": digest, "utf8_bom": raw.startswith(b"\xef\xbb\xbf"),
            "total_lines": len(lines), "lines": page,
            "truncated": next_line is not None, "next_line": next_line, "next_column": next_column}


def replace_text(text: str, edits: list[TextEdit]) -> str:
    if not 1 <= len(edits) <= 50:
        raise ToolException("edits 必须包含 1..50 个替换")
    spans = []
    for edit in edits:
        if not edit.old_text:
            raise ToolException("old_text 不能为空；新建文件请用 write_file")
        start = text.find(edit.old_text)
        if start < 0:
            raise ToolException("旧内容不匹配，未修改；请重新读取后重试")
        if text.find(edit.old_text, start + 1) >= 0:
            raise ToolException("旧内容存在多处匹配，未修改；请增加上下文使匹配唯一")
        spans.append((start, start + len(edit.old_text), edit.new_text))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ToolException("替换区间重叠，未修改；请合并重叠的 edits")
    updated = text
    for start, end, replacement in reversed(spans):
        updated = updated[:start] + replacement + updated[end:]
    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolException("替换后文件超过大小上限，未修改")
    if "\0" in updated:
        raise ToolException("替换内容含 NUL，未修改")
    return updated


def atomic_replace(target: Path, original: bytes, updated: bytes) -> None:
    """同目录临时文件保留权限；提交前重查字节和路径，避免部分写入。"""
    temporary = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".focus-edit-", delete=False) as stream:
            temporary = Path(stream.name)
            if os.name == "nt":
                # 在写入任何内容前复制 DACL，避免暂存文件继承更宽的目录访问权限。
                _copy_windows_dacl(target, temporary)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        if target.resolve() != target or read_utf8(target)[0] != original:
            raise ToolException("文件在编辑期间变化，未修改；请重新读取后重试")
        os.replace(temporary, target)
    except OSError as error:
        raise ToolException(f"写入失败，未完成替换 {target}: {error}") from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
            temporary.unlink(missing_ok=True)


def _copy_windows_dacl(source: Path, destination: Path) -> None:
    """复制 DACL 和继承保护位；失败即终止，不放宽安全描述符后继续写入。"""
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = api.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    size = wintypes.DWORD()
    get_security(str(source), 4, None, 0, ctypes.byref(size))  # DACL_SECURITY_INFORMATION
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(size.value)
    if not get_security(str(source), 4, descriptor, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    control, revision = wintypes.WORD(), wintypes.DWORD()
    get_control = api.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    # SE_DACL_PROTECTED -> PROTECTED/UNPROTECTED_DACL_SECURITY_INFORMATION。
    flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
    present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
    get_dacl = api.GetSecurityDescriptorDacl
    get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_dacl.restype = wintypes.BOOL
    if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
        raise ctypes.WinError(ctypes.get_last_error())
    set_security = api.SetNamedSecurityInfoW
    set_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_security.restype = wintypes.DWORD
    error = set_security(str(destination), 1, flags, None, None, dacl, None)  # SE_FILE_OBJECT
    if error:
        raise ctypes.WinError(error)


def edit_feedback(target: Path, original: bytes, updated: bytes) -> dict:
    chunks = difflib.unified_diff(
        [line + ending for line, ending in text_lines(original.decode("utf-8"))],
        [line + ending for line, ending in text_lines(updated.decode("utf-8"))],
        fromfile=str(target), tofile=str(target), n=3,
    )
    shown = ""
    truncated = False
    for chunk in chunks:
        if not chunk.endswith("\n"):
            chunk += "\n\\ No newline at end of file\n"
        room = MAX_OUTPUT_CHARS - len(shown)
        shown += chunk[:room]
        if len(chunk) > room:
            truncated = True
            break
    return {"path": str(target), "changed": original != updated,
            "before_sha256": sha256(original).hexdigest(), "sha256": sha256(updated).hexdigest(),
            "diff": shown, "diff_truncated": truncated}
