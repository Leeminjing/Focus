"""本文件对外提供 PreparedWorkspaceRegistry，登记仍带常驻 Windows ACL 调整的工作区。

输入为真实工作区路径及本机注册表文件；输出为持久的已准备根清单。
具体工作流为在授权成功后以原子替换写入去重列表，查询时读取保存内容；
记录只用于告知持久影响，不参与本次令牌授权或决定文件访问。
示例：registry.record(Path("C:/ws"))；registry.list() 返回此前准备过的真实路径。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from focus.sandbox.contracts import SandboxUnavailable


class PreparedWorkspaceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        self._path = path or base / "Focus" / "prepared-sandbox-workspaces.json"
        self._lock = threading.RLock()

    def record(self, workspace: Path) -> None:
        root = str(workspace.resolve(strict=True))
        with self._lock:
            roots = set(self.list())
            if root in roots:
                return
            roots.add(root)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(json.dumps(sorted(roots), ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, self._path)
            except OSError as error:
                raise SandboxUnavailable(f"无法登记已准备的工作区 {root}: {error}") from error

    def list(self) -> tuple[str, ...]:
        with self._lock:
            if not self._path.exists():
                return ()
            try:
                values = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise SandboxUnavailable(f"已准备工作区登记不可读: {error}") from error
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise SandboxUnavailable("已准备工作区登记格式无效")
            return tuple(values)
