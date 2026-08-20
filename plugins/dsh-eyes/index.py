"""dsh-eyes 附件索引:按 thread_id 分桶,索引只存 ref,图片字节落盘附件目录。

对外提供:
    AttachmentIndex — 会话隔离的附件索引(跨 run/压缩/重启有效)

字节载体(对齐 dsh-eyes「附件服务持久字节、索引只存 ref」):
    index.json 条目 SHALL 为 {"file": "attachments/<id>.<ext>", "added_at"}(小对象);
    图片字节 SHALL 解码后写入 data/attachments/<id>.<ext>;
    旧格式(条目直接含 data URL)SHALL 兼容读取。

实现说明:
    data/index.json 形如 {"<thread_id>": {"<attachment_id>": {"file": …, "added_at": …}}};
    进程内 asyncio.Lock 串行读写,写入原子替换;文件损坏时丢弃重建(缓存性质,不阻塞主流程)。
    ponytail: 单进程 asyncio 锁足够(桌面单进程形态);多进程部署出现写冲突时再换锁方案。
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"
_ATTACH_DIR = _DATA_DIR / "attachments"
_INDEX_FILE = _DATA_DIR / "index.json"

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}


def parse_data_url(data_url: str) -> tuple[str, bytes] | None:
    """data URL → (mime, bytes);格式非法返回 None。"""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep:
        return None
    mime = "image/png"
    if ";" in header:
        media, params = header.split(";", 1)
        mime = media.split(":", 1)[1]
        if "base64" not in params:
            return None
    else:
        mime = header.split(":", 1)[1]
        return None  # 仅支持 base64 编码
    try:
        return mime, base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None


def _ext_for_mime(mime: str) -> str:
    for ext, candidate in _MIME_BY_EXT.items():
        if candidate == mime:
            return ext
    return ".png"


class AttachmentIndex:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, dict]] = {}

    def load(self) -> None:
        """启动时读索引文件;损坏/缺失时从空重建。"""
        if not _INDEX_FILE.is_file():
            return
        try:
            raw = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {
                    str(thread): {
                        str(attachment): dict(entry)
                        for attachment, entry in entries.items()
                        if isinstance(entry, dict)
                    }
                    for thread, entries in raw.items()
                    if isinstance(entries, dict)
                }
        except (OSError, json.JSONDecodeError):
            logger.warning("dsh-eyes: 附件索引损坏,已从空重建", exc_info=True)
            self._data = {}

    async def attach_bytes(
        self, thread_id: str, attachment_id: str, data_url: str, max_bytes: int,
    ) -> str:
        """解码 data URL 并把字节落盘附件目录;返回附件文件名(幂等)。"""
        parsed = parse_data_url(data_url)
        if parsed is None:
            raise ValueError("无法解析图片 data URL")
        mime, payload = parsed
        if len(payload) > max_bytes:
            raise ValueError(
                f"图片 {len(payload)} 字节超过上限 {max_bytes} 字节"
            )
        filename = f"{attachment_id}{_ext_for_mime(mime)}"
        target = _ATTACH_DIR / filename
        async with self._lock:
            entry = self._data.setdefault(thread_id, {}).get(attachment_id)
            if entry is None or "url" in entry:  # 新登记或旧格式升级为落盘
                _ATTACH_DIR.mkdir(parents=True, exist_ok=True)
                if not target.is_file():
                    fd, tmp_path = tempfile.mkstemp(dir=str(_ATTACH_DIR), suffix=".tmp")
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(payload)
                        os.replace(tmp_path, target)
                    except OSError:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
            self._data.setdefault(thread_id, {})[attachment_id] = {
                "file": f"attachments/{filename}",
                "mime": mime,
                "added_at": _now_iso(),
            }
            await self._save()
            return filename

    def get(self, thread_id: str, attachment_id: str) -> dict | None:
        """取回附件条目;不属于当前会话返回 None(会话隔离)。"""
        entry = self._data.get(thread_id, {}).get(attachment_id)
        return dict(entry) if entry else None

    def resolve_data_url(self, thread_id: str, attachment_id: str) -> str | None:
        """还原 data URL:旧格式条目直接返回 url;新格式读落盘字节编码。"""
        entry = self.get(thread_id, attachment_id)
        if entry is None:
            return None
        if "url" in entry:
            return str(entry["url"])
        path = _DATA_DIR / str(entry.get("file", ""))
        if not path.is_file():
            logger.warning("dsh-eyes: 附件字节文件缺失: %s", path)
            return None
        mime = entry.get("mime") or "image/png"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    def next_number(self, thread_id: str) -> int:
        """返回该会话的下一个图片序号(用于 【图片N】 引用)。"""
        return len(self._data.get(thread_id, {})) + 1

    async def _save(self) -> None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False)
            os.replace(tmp_path, _INDEX_FILE)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_index: AttachmentIndex | None = None


def get_index() -> AttachmentIndex:
    """进程内共享索引单例(entry 与工具共用)。"""
    global _index
    if _index is None:
        _index = AttachmentIndex()
        _index.load()
    return _index
