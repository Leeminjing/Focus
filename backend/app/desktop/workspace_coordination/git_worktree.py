r"""本文件对外提供 GitWorktreeIsolationProvider 安全隔离写实现。

输入为已解析 source/target 根、存在的共同 baseline、Loop/Lane owner 与配额；输出为通过 Git worktree
创建的隔离 slot 结果。具体工作流为验证 source 是仓库、target 位于受管根且未存在、核对配额，调用
工作线程中的非 shell Git 创建 worktree，并保存主仓库路径作为清理句柄以避免 Windows 从待删目录执行命令。
示例：`result = await provider.prepare(request)`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

from backend.app.desktop.workspace_coordination.isolation import IsolationRequest, IsolationResult


class GitWorktreeIsolationProvider:
    def __init__(self, managed_root: Path, quota: int = 8) -> None:
        self._root = managed_root.resolve()
        self._quota = quota

    async def prepare(self, request: IsolationRequest) -> IsolationResult:
        source = request.source_root.resolve(strict=True)
        target = request.target_root.resolve(strict=False)
        self._validate_target(target)
        await self._git(source, "rev-parse", "--is-inside-work-tree")
        await self._git(source, "cat-file", "-e", f"{request.baseline}^{{commit}}")
        if target.exists():
            raise ValueError("隔离 worktree 目标已存在")
        if self._active_count() >= self._quota:
            raise RuntimeError("隔离 workspace 配额已用尽")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._git(source, "worktree", "add", "--detach", str(target), request.baseline)
        return IsolationResult(
            root_path=target,
            provider="git_worktree",
            baseline=request.baseline,
            cleanup_token=str(source),
        )

    async def remove(self, result: IsolationResult) -> None:
        target = result.root_path.resolve(strict=False)
        self._validate_target(target)
        if not target.exists():
            return
        source = Path(result.cleanup_token).resolve(strict=True)
        await self._git(source, "worktree", "remove", "--force", str(target))

    def _validate_target(self, target: Path) -> None:
        if target == self._root or self._root not in target.parents:
            raise ValueError("隔离 worktree 必须位于受管根的子目录")

    def _active_count(self) -> int:
        if not self._root.exists():
            return 0
        return sum(1 for path in self._root.glob("*/*") if path.is_dir())

    @staticmethod
    async def _git(root: Path, *arguments: str) -> str:
        process = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "git worktree 操作失败")
        return process.stdout.strip()
