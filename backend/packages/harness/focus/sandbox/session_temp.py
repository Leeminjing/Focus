"""本文件对外提供 SessionTempRegistry，管理执行会话和真实工作区对应的私有临时授权。

输入为不可变调用绑定、固定 Node 运行时、Focus 授权适配器和系统临时根。
输出为带独立 SID 的私有临时目录；释放时返回清理告警且保留常驻工作区授权。
具体工作流为首次调用创建继承普通 NTFS ACL 的独立目录并应用授权，后续同组合复用，
服务释放时先撤销临时授权再删目录；重启后的新注册表自然分配新目录。
示例：grant = registry.prepare(binding, node, helper, temp_root)；registry.close(node, helper)。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from focus.sandbox.contracts import SandboxUnavailable
from focus.security.execution import CallExecutionBinding
from focus.security.paths import is_within


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionTempGrant:
    workspace: Path
    temp_dir: Path
    write_sid: str
    temp_write_sid: str


class SessionTempRegistry:
    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], SessionTempGrant] = {}
        self._lock = threading.RLock()
        self._closed = False

    def prepare(
        self, binding: CallExecutionBinding, node: str, helper: Path, temp_root: Path,
    ) -> SessionTempGrant:
        key = (binding.session_id, str(binding.workspace).casefold())
        with self._lock:
            if self._closed:
                raise SandboxUnavailable("私有临时授权服务已关闭")
            existing = self._grants.get(key)
            if existing is not None:
                return existing
            if is_within(binding.workspace, temp_root):
                raise SandboxUnavailable("工作区包含临时根，拒绝重叠授权")
            temp_dir = temp_root / f"focus-sandbox-{uuid.uuid4().hex}"
            try:
                temp_dir.mkdir()
                data = self._invoke(node, helper, "prepare", binding.workspace, temp_dir)
                grant = SessionTempGrant(
                    workspace=Path(data["workspace"]), temp_dir=Path(data["temp"]),
                    write_sid=str(data["writeSid"]), temp_write_sid=str(data["tempWriteSid"]),
                )
                if (
                    grant.write_sid == grant.temp_write_sid
                    or grant.workspace.resolve(strict=True) != binding.workspace
                    or grant.temp_dir.resolve(strict=True) != temp_dir.resolve(strict=True)
                ):
                    raise SandboxUnavailable("Windows ACL 授权适配器返回无效边界")
                self._grants[key] = grant
                return grant
            except Exception as error:
                if temp_dir.exists():
                    try:
                        self._invoke(node, helper, "release", binding.workspace, temp_dir)
                    except Exception as cleanup_error:
                        _log.warning("Windows sandbox temp grant cleanup: %s", cleanup_error)
                    try:
                        self._remove_owned(temp_dir, temp_root)
                    except Exception as cleanup_error:
                        _log.warning("Windows sandbox private temp cleanup: %s", cleanup_error)
                if isinstance(error, SandboxUnavailable):
                    raise
                raise SandboxUnavailable(f"Windows ACL 私有授权准备失败: {error}") from error

    def close(self, node: str, helper: Path, temp_root: Path) -> tuple[str, ...]:
        with self._lock:
            self._closed = True
            grants = tuple(self._grants.values())
            self._grants.clear()
        warnings: list[str] = []
        for grant in grants:
            try:
                self._invoke(node, helper, "release", grant.workspace, grant.temp_dir)
            except Exception as error:
                warnings.append(f"撤销临时授权失败 {grant.temp_dir}: {error}")
            try:
                self._remove_owned(grant.temp_dir, temp_root)
            except OSError as error:
                warnings.append(f"删除私有临时目录失败 {grant.temp_dir}: {error}")
        return tuple(warnings)

    @staticmethod
    def _invoke(node: str, helper: Path, action: str, workspace: Path, temp_dir: Path) -> dict:
        try:
            process = subprocess.run(
                [node, str(helper), action, str(workspace), str(temp_dir)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxUnavailable(f"Windows ACL 授权适配器不可用: {error}") from error
        if process.returncode != 0:
            raise SandboxUnavailable(f"Windows ACL 授权失败: {process.stderr.strip()}")
        try:
            value = json.loads(process.stdout)
        except (ValueError, TypeError) as error:
            raise SandboxUnavailable("Windows ACL 授权适配器返回无效结果") from error
        fields = ("released",) if action == "release" else ("workspace", "temp", "writeSid", "tempWriteSid")
        if not isinstance(value, dict) or not all(
            isinstance(value.get(name), str) and value[name] for name in fields
        ):
            raise SandboxUnavailable("Windows ACL 授权适配器返回无效边界字段")
        return value

    @staticmethod
    def _remove_owned(temp_dir: Path, temp_root: Path) -> None:
        if temp_dir.resolve().parent != temp_root.resolve() or not temp_dir.name.startswith("focus-sandbox-"):
            raise SandboxUnavailable("拒绝清理非私有临时目录")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
