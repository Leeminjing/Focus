"""
本文件对外提供工作区内置工具（原沙箱工具改造迁移），供桌面 Agent 在真实宿主机工作区执行。

对外提供:
    WORKSPACE_TOOLS — 全部工作区内置工具列表（read_file/list_files/write_file/bash/powershell/cmd/sh）
    TOOL_NAMES_BY_PERMISSION — 权限 → 工具名映射（read/write/host_command）
    select_workspace_tools(permissions) — 按权限过滤工具

输入:
    所有工具声明 `runtime: ToolRuntime` 参数，由 LangGraph 注入；per-run 的
    `workspace`（真实工作区路径）与 `permissions`（read/write/host_command）经
    `runtime.context` 获取——worker 已将 langgraph_context 透传给 agent.astream(context=...)。

输出:
    read_file/list_files → 文件文本/目录列表
    write_file → 写入结果说明
    bash/powershell/cmd/sh → 命令执行输出（截断至 30000 字符）

具体工作流:
    (1) 工具调用时从 runtime.context 读取 workspace 与 permissions
    (2) 权限门控：未授权（如无 write 时调用 write_file）→ PermissionError
    (3) 路径 containment 校验：绝对路径或相对工作区路径，解析后必须位于工作区内
    (4) read_file 按扩展名分发：.pdf/.docx/.doc → focus.readers 解析，其余 UTF-8 读取
    (5) shell 工具：subprocess 在工作区目录下执行（timeout=120）

示例:
    tools = select_workspace_tools(["read", "write"])
    # → [read_file, list_files, write_file]
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool

WORKSPACE_TOOL_NAMES = frozenset({"read_file", "list_files", "write_file", "bash", "powershell", "cmd", "sh"})

TOOL_NAMES_BY_PERMISSION: dict[str, list[str]] = {
    "read": ["read_file", "list_files"],
    "write": ["write_file"],
    "host_command": ["bash", "powershell", "cmd", "sh"],
}


def _runtime_values(runtime: ToolRuntime) -> tuple[Path, frozenset[str]]:
    """从 runtime.context 提取工作区路径与权限集合。"""
    context = runtime.context
    workspace = context.get("workspace") if isinstance(context, dict) else None
    if not workspace:
        raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
    raw_permissions = context.get("permissions") if isinstance(context, dict) else None
    permissions = frozenset(["read"] if raw_permissions is None else raw_permissions)
    return Path(workspace).resolve(), permissions


def _canonical_path_text(path: Path) -> str:
    """统一 Windows 普通路径与扩展路径前缀的等价表示。"""
    value = str(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _is_workspace_path(root: Path, target: Path) -> bool:
    root_text = _canonical_path_text(root)
    target_text = _canonical_path_text(target)
    try:
        return os.path.commonpath((root_text, target_text)) == root_text
    except ValueError:
        return False


def _resolve_workspace_path(root: Path, value: str) -> Path:
    """解析工具路径并校验位于工作区内。"""
    candidate = Path(value).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not _is_workspace_path(root, target):
        raise PermissionError(f"路径不属于当前工作区: {value}")
    return target


@tool
def read_file(path: str, runtime: ToolRuntime) -> str:
    """读取当前工作区内文件；path 可以是绝对路径或相对工作区路径，支持 .pdf/.docx/.doc。"""
    workspace, permissions = _runtime_values(runtime)
    if "read" not in permissions:
        raise PermissionError("当前运行未授权 read")
    target = _resolve_workspace_path(workspace, path)
    if target.is_dir():
        raise ToolException(f"目标是目录，请改用 list_files: {target}")
    if not target.is_file():
        raise ToolException(f"文件不存在: {target}")
    suffix = target.suffix.lower()
    if suffix in {".pdf", ".docx", ".doc"}:
        from focus.readers import _read_doc, _read_docx, _read_pdf

        if suffix == ".pdf":
            return _read_pdf(str(target))
        if suffix == ".docx":
            return _read_docx(str(target))
        return _read_doc(str(target))
    return target.read_text(encoding="utf-8", errors="replace")


@tool
def list_files(path: str, runtime: ToolRuntime) -> str:
    """列出当前工作区内目录；path 可以是绝对路径或相对工作区路径。"""
    workspace, permissions = _runtime_values(runtime)
    if "read" not in permissions:
        raise PermissionError("当前运行未授权 read")
    target = _resolve_workspace_path(workspace, path)
    if target.is_file():
        raise ToolException(f"目标是文件，请改用 read_file: {target}")
    if not target.is_dir():
        raise ToolException(f"目录不存在: {target}")
    return "\n".join(str(item) for item in sorted(target.iterdir()))


def _recoverable_path_error(error: ToolException) -> str:
    return f"工作区工具调用失败：{error}"


read_file.handle_tool_error = _recoverable_path_error
list_files.handle_tool_error = _recoverable_path_error


@tool
def write_file(path: str, content: str, runtime: ToolRuntime) -> str:
    """在当前工作区写入 UTF-8 文本；只有用户授权 write 时才会被装配。"""
    workspace, permissions = _runtime_values(runtime)
    if "write" not in permissions:
        raise PermissionError("当前运行未授权 write")
    target = _resolve_workspace_path(workspace, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入真实宿主机路径: {target}"


def _run_shell(command: str, runtime: ToolRuntime, exe_name: str, args: list[str]) -> str:
    """在工作区内执行 shell 命令（权限门控 + subprocess）。"""
    workspace, permissions = _runtime_values(runtime)
    if "host_command" not in permissions:
        raise PermissionError("当前运行未授权 host_command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("命令不能为空")
    exe_path = shutil.which(exe_name)
    if exe_path is None:
        raise RuntimeError(f"Shell '{exe_name}' not found in PATH")
    result = subprocess.run(
        [exe_path, *args, command],
        cwd=workspace, capture_output=True, text=True, timeout=120, shell=False,
        errors="replace",
    )
    return (result.stdout + result.stderr)[-30000:]


@tool
def bash(command: str, runtime: ToolRuntime) -> str:
    """在工作区目录下执行 Bash 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "bash", ["-c"])


@tool
def sh(command: str, runtime: ToolRuntime) -> str:
    """在工作区目录下执行 POSIX sh 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "sh", ["-c"])


@tool
def cmd(command: str, runtime: ToolRuntime) -> str:
    """在工作区目录下执行 Windows CMD 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "cmd.exe", ["/c"])


@tool
def powershell(command: str, runtime: ToolRuntime) -> str:
    """在工作区目录下执行 PowerShell 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "powershell.exe", ["-NoProfile", "-Command"])


WORKSPACE_TOOLS: list[Any] = [read_file, list_files, write_file, bash, powershell, cmd, sh]


def select_workspace_tools(permissions: list[str]) -> list[Any]:
    """按权限过滤工作区内置工具。

    输入:
        permissions: list[str] — 授权权限（read/write/host_command）

    输出:
        list[Any] — 该权限下可装配的工具列表

    工作流:
        (1) 归一化权限并去重
        (2) read → read_file/list_files；write → write_file；host_command → 4 个 shell
    """
    normalized = list(dict.fromkeys(["read"] if permissions is None else permissions))
    names: set[str] = set()
    for permission in normalized:
        names.update(TOOL_NAMES_BY_PERMISSION.get(permission, []))
    return [tool_ for tool_ in WORKSPACE_TOOLS if tool_.name in names]
