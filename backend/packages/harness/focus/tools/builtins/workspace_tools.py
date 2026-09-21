"""本文件对外提供工作区内置工具，供桌面 Agent 在真实宿主机工作区执行。

对外提供:
    WORKSPACE_TOOLS — 全部工作区内置工具列表（read_file/list_files/write_file/bash/powershell/cmd/sh）
    TOOL_NAMES_BY_PERMISSION — 权限 → 工具名映射（read/write/host_command）
    select_workspace_tools(permissions) — 按权限过滤工具

输入:
    所有工具声明 `runtime: ToolRuntime` 参数，由 LangGraph 注入；每次运行的 `workspace`
    与 `permissions`（read/write/host_command）以及 `access_mode` 经 `runtime.context`
    提供，由执行层在运行起点派生，工具自身不拼装这些字段。

输出:
    read_file/list_files → 文件文本/目录列表
    write_file → 写入结果说明
    bash/powershell/cmd/sh → 命令执行输出（截断至 30000 字符）

具体工作流:
    (1) 工具调用时经 focus.security 构造访问策略并读取权限集合
    (2) 权限门控：未授权（如无 write 时调用 write_file）→ PermissionError
    (3) 路径解释委托 focus.security；准入判定不在这里——唯一准入点是最外层中间件，
        本文件因此只做路径解释与 IO，判定与参数改写由 AccessPolicyMiddleware 完成
    (4) read_file 按内容分发：.pdf/.docx/.doc → focus.readers 解析；图片与二进制内容
        → 抛可修正的 ToolException（绝不静默返回替换字符乱码）；其余按 UTF-8 读取
    (5) shell 工具以工作区为执行目录启动子进程（timeout=120）。宿主命令的本地副作用在
        执行前无法完整证明，按不透明效果处理并交由准入中间件逐次交回人类批准；
        本文件不尝试通过分析命令文本来判断其是否会越界

效果声明：三个文件工具的目标就是参数里的 path，可由确定性解析完整枚举，故声明为可结构化
枚举；四个 shell 的副作用无法在执行前证明，显式声明为不透明（不依赖默认值，使分类可审计）。

示例:
    tools = select_workspace_tools(["read", "write"])
    # → [read_file, list_files, write_file]
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool

from focus.images import is_image_name
from focus.security import AccessPolicy, canonical_target, policy_from_context
from focus.security.effects import (
    OPAQUE_LOCAL_EFFECT,
    ResolvedFsEffect,
    declare_all_effects,
    declare_effect,
    structured_fs,
)

WORKSPACE_TOOL_NAMES = frozenset({"read_file", "list_files", "write_file", "bash", "powershell", "cmd", "sh"})

TOOL_NAMES_BY_PERMISSION: dict[str, list[str]] = {
    "read": ["read_file", "list_files"],
    "write": ["write_file"],
    "host_command": ["bash", "powershell", "cmd", "sh"],
}


def _runtime_values(runtime: ToolRuntime[dict]) -> tuple[AccessPolicy, frozenset[str]]:
    """从 runtime.context 提取访问策略与权限集合。"""
    context = runtime.context
    policy = policy_from_context(context)
    raw_permissions = context.get("permissions") if isinstance(context, dict) else None
    permissions = frozenset(["read"] if raw_permissions is None else raw_permissions)
    return policy, permissions


def _interpret_target(policy: AccessPolicy, value: str) -> Path:
    """把路径值解释为真实目标。

    只做路径解释与 IO；是否允许访问由唯一准入点（AccessPolicyMiddleware）判定，
    因此本文件不出现任何准入调用。
    """
    return canonical_target(policy.workspace, value)


def _declared_target(args: Mapping[str, Any], context: Mapping[str, Any]) -> Path:
    """解析一次调用唯一的结构化本地目标；解析基准由受治理上下文提供。"""
    policy = policy_from_context(context)
    return canonical_target(policy.workspace, str(args.get("path") or ""))


def _stamped_args(args: Mapping[str, Any], target: Path) -> Mapping[str, Any]:
    """把目标参数替换为规范化真实路径，随解析结果一并交给准入点下发。"""
    return {**args, "path": str(target)}


def _read_targets(args: Mapping[str, Any], context: Mapping[str, Any]) -> ResolvedFsEffect:
    target = _declared_target(args, context)
    return ResolvedFsEffect(reads=(target,), args=_stamped_args(args, target))


def _write_targets(args: Mapping[str, Any], context: Mapping[str, Any]) -> ResolvedFsEffect:
    target = _declared_target(args, context)
    return ResolvedFsEffect(writes=(target,), args=_stamped_args(args, target))


@tool
def read_file(path: str, runtime: ToolRuntime[dict]) -> str:
    """读取当前工作区内文件；path 可以是绝对路径或相对工作区路径，支持 .pdf/.docx/.doc。"""
    policy, permissions = _runtime_values(runtime)
    if "read" not in permissions:
        raise PermissionError("当前运行未授权 read")
    target = _interpret_target(policy, path)
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
    raw = target.read_bytes()
    if is_image_name(target.name):
        raise ToolException(
            f"{target.name} 是图片材料，无法按文本读取；其内容只能由具备图像输入能力的模型处理"
        )
    if _looks_binary(raw):
        raise ToolException(
            f"{target.name} 是二进制文件，无法按文本读取；请改用能解析该格式的工具"
        )
    return raw.decode("utf-8", errors="replace")


def _looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:8192]


@tool
def list_files(path: str, runtime: ToolRuntime[dict]) -> str:
    """列出当前工作区内目录；path 可以是绝对路径或相对工作区路径。"""
    policy, permissions = _runtime_values(runtime)
    if "read" not in permissions:
        raise PermissionError("当前运行未授权 read")
    target = _interpret_target(policy, path)
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
def write_file(path: str, content: str, runtime: ToolRuntime[dict]) -> str:
    """在当前工作区写入 UTF-8 文本；只有用户授权 write 时才会被装配。"""
    policy, permissions = _runtime_values(runtime)
    if "write" not in permissions:
        raise PermissionError("当前运行未授权 write")
    target = _interpret_target(policy, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入真实宿主机路径: {target}"


def _run_shell(command: str, runtime: ToolRuntime[dict], exe_name: str, args: list[str]) -> str:
    """以工作区为执行目录启动一个宿主命令子进程（权限门控 + subprocess）。"""
    policy, permissions = _runtime_values(runtime)
    if "host_command" not in permissions:
        raise PermissionError("当前运行未授权 host_command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("命令不能为空")
    exe_path = shutil.which(exe_name)
    if exe_path is None:
        raise RuntimeError(f"Shell '{exe_name}' not found in PATH")
    result = subprocess.run(
        [exe_path, *args, command],
        cwd=policy.workspace, capture_output=True, text=True, timeout=120, shell=False,
        errors="replace",
    )
    return (result.stdout + result.stderr)[-30000:]


@tool
def bash(command: str, runtime: ToolRuntime[dict]) -> str:
    """在工作区目录下执行 Bash 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "bash", ["-c"])


@tool
def sh(command: str, runtime: ToolRuntime[dict]) -> str:
    """在工作区目录下执行 POSIX sh 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "sh", ["-c"])


@tool
def cmd(command: str, runtime: ToolRuntime[dict]) -> str:
    """在工作区目录下执行 Windows CMD 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "cmd.exe", ["/c"])


@tool
def powershell(command: str, runtime: ToolRuntime[dict]) -> str:
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


READ_TARGET_EFFECT = structured_fs(_read_targets)
WRITE_TARGET_EFFECT = structured_fs(_write_targets)

declare_effect(read_file, READ_TARGET_EFFECT)
declare_effect(list_files, READ_TARGET_EFFECT)
declare_effect(write_file, WRITE_TARGET_EFFECT)
declare_all_effects([bash, powershell, cmd, sh], OPAQUE_LOCAL_EFFECT)
