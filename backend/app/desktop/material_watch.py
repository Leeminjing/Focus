"""材料轮询的文件状态缓存；由 watcher 串行在线程中调用，最多一个文件操作在途。

首次观察核对摘要，之后仅在元数据连续两轮稳定且发生变化时重新计算。
读取期间再次变化的文件留待后续轮询，删除文件交给宿主恢复，取消保护的记录被清理。
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class _Observation:
    signature: tuple[int, ...]
    digest: str | None


class MaterialFileMonitor:
    def __init__(self, digest: Callable[[Path], str]) -> None:
        self._digest = digest
        self._observations: dict[tuple[str, Path], _Observation] = {}

    def retain(self, active: set[tuple[str, Path]]) -> None:
        self._observations = {
            key: value for key, value in self._observations.items() if key in active
        }

    @staticmethod
    def _signature(path: Path) -> tuple[int, ...]:
        info = path.stat()
        return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_dev, info.st_ino)

    def digest(self, material_id: str, path: Path) -> str | None:
        """返回稳定内容的摘要；None 表示仍在合并变更，缺失文件抛 FileNotFoundError。"""
        key = (material_id, path)
        try:
            signature = self._signature(path)
        except FileNotFoundError:
            self._observations.pop(key, None)
            raise
        previous = self._observations.get(key)
        if previous is not None:
            if previous.signature != signature:
                self._observations[key] = _Observation(signature, None)
                return None
            if previous.digest is not None:
                return previous.digest

        digest = self._digest(path)
        after = self._signature(path)
        if after != signature:
            self._observations[key] = _Observation(after, None)
            return None
        self._observations[key] = _Observation(signature, digest)
        return digest


def restore_material_file(path: Path, content: bytes) -> str:
    """在线程中执行恢复写入及摘要计算。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()
