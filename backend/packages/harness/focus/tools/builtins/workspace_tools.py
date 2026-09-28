"""本文件对外提供工作区内置工具，供桌面 Agent 在真实宿主机工作区执行。

对外提供:
    WORKSPACE_TOOLS — 全部工作区内置工具列表（read_file/list_files/write_file/bash/powershell/cmd/sh）
    TOOL_NAMES_BY_PERMISSION — 权限 → 工具名映射（read/write/host_command）
    select_workspace_tools(permissions) — 按权限过滤工具

输入:
    所有工具声明 `runtime: ToolRuntime` 参数，由 LangGraph 注入；每次运行的 `workspace`
    与 `permissions`（read/write/host_command）以及 `access_mode` 经签发的 SecurityContext
    提供，由执行层在运行起点派生，工具自身不接受扁平字段充当授权。

输出:
    read_file/list_files → 文件文本/目录列表
    write_file → 写入结果说明
    bash/powershell/cmd/sh → 含实际模式、受限状态、退出状态及截断标记的执行事实

具体工作流:
    (1) 工具调用时验证 SecurityContext 和现存工作区，从签发身份构造策略并读取权限集合
    (2) 权限门控：未授权（如无 write 时调用 write_file）→ PermissionError
    (3) 路径解释委托 focus.security；准入判定不在这里——唯一准入点是最外层中间件，
        本文件因此只做路径解释与 IO，判定与参数改写由 AccessPolicyMiddleware 完成
    (4) read_file 按内容分发：.pdf/.docx/.doc → focus.readers 解析；图片与二进制内容
        → 抛可修正的 ToolException（绝不静默返回替换字符乱码）；其余按 UTF-8 读取
    (5) Shell 先排除 Windows WSL 启动器，再绑定服务端执行身份和本次调用；受限模式经固定 Windows ACL 后端执行，
        完全访问模式明确跳过文件沙箱；后端失败时不启动未受限替代命令

效果声明：三个文件工具的目标就是参数里的 path，可由确定性解析完整枚举，故声明为可结构化
枚举；四个 Shell 仅因实际走受限后端而声明受限效果，未知宿主工具仍按不透明效果审批。

示例:
    tools = select_workspace_tools(["read", "write"])
    # → [read_file, list_files, write_file]
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool

from focus.images import is_image_name
from focus.sandbox import SandboxUnavailable, ShellExecutionRequest, WindowsAclBackend
from focus.sandbox.diagnostics import classify_shell_outcome
from focus.security import AccessPolicy, canonical_target, policy_from_context
from focus.security.execution import bind_call_execution
from focus.security.context import security_context_of
from focus.security.file_target import checked_write_target
from focus.security.policy import AccessMode
from focus.security.effects import (
    SANDBOXED_SHELL_EFFECT,
    ResolvedFsEffect,
    declare_all_effects,
    declare_effect,
    structured_fs,
)

WORKSPACE_TOOL_NAMES = frozenset({"read_file", "list_files", "write_file", "bash", "powershell", "cmd", "sh"})
_SHELL_BACKEND = WindowsAclBackend()
atexit.register(_SHELL_BACKEND.close)

TOOL_NAMES_BY_PERMISSION: dict[str, list[str]] = {
    "read": ["read_file", "list_files"],
    "write": ["write_file"],
    "host_command": ["bash", "powershell", "cmd", "sh"],
}


def _runtime_values(runtime: ToolRuntime[dict]) -> tuple[AccessPolicy, frozenset[str]]:
    """从服务端签发的执行身份提取策略与工具能力。"""
    security = security_context_of(runtime.context)
    workspace = security.authorization.workspace
    if not workspace.is_dir():
        raise RuntimeError(f"实际工作区不存在或不是目录: {workspace}")
    return security.policy(), frozenset(security.authorization.permissions)


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
def write_file(
    path: str, content: str, runtime: ToolRuntime[dict],
    requested_mode: str | None = None, reason: str | None = None,
) -> str:
    """在当前工作区写入 UTF-8 文本；只有用户授权 write 时才会被装配。"""
    policy, permissions = _runtime_values(runtime)
    if "write" not in permissions:
        raise PermissionError("当前运行未授权 write")
    target = checked_write_target(policy, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = checked_write_target(policy, str(target))
    target.write_text(content, encoding="utf-8")
    return f"已写入真实宿主机路径: {target}"


def _run_shell(command: str, runtime: ToolRuntime[dict], exe_name: str, args: list[str]) -> str:
    """按服务端绑定的本次模式执行内置 Shell。"""
    _, permissions = _runtime_values(runtime)
    if "host_command" not in permissions:
        raise PermissionError("当前运行未授权 host_command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("命令不能为空")
    exe_path = shutil.which(exe_name)
    if exe_path is None:
        raise RuntimeError(f"Shell '{exe_name}' not found in PATH")
    if _is_wsl_launcher(exe_path):
        raise RuntimeError("UNSUPPORTED_SHELL: Windows WSL 启动器不属于本机文件沙箱范围")
    binding = bind_call_execution(runtime.context, str(runtime.tool_call_id or ""))
    cancel_event = security_context_of(runtime.context).extras.get("sandbox_cancel_event")
    if binding.mode is AccessMode.DANGER_FULL_ACCESS:
        try:
            process = subprocess.run(
                [exe_path, *args, command], cwd=binding.workspace,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=120, shell=False, errors="replace", close_fds=True,
            )
        except subprocess.TimeoutExpired as error:
            return _shell_fact(binding, False, "timeout", None, str(error.stdout or ""), str(error.stderr or ""))
        return _shell_fact(binding, False, "exited", process.returncode, process.stdout, process.stderr)
    try:
        result = _SHELL_BACKEND.run(
            ShellExecutionRequest(binding, exe_path, (*args, command), cancel_event=cancel_event)
        )
    except SandboxUnavailable as error:
        return _shell_fact(binding, False, "SANDBOX_UNAVAILABLE", None, "", str(error))
    return _shell_fact(
        binding, result.backend_applied, result.status, result.exit_code,
        result.stdout, result.stderr,
    )


def _is_wsl_launcher(executable: str) -> bool:
    if os.name != "nt":
        return False
    path = Path(executable).resolve()
    if path.name.casefold() not in {"bash.exe", "wsl.exe"}:
        return False
    windows = Path(os.environ.get("WINDIR", "C:/Windows")).resolve()
    return path.parent in {(windows / name).resolve() for name in ("System32", "SysWOW64", "Sysnative")}


def _shell_fact(binding: Any, applied: bool, status: str, exit_code: int | None, stdout: str, stderr: str) -> str:
    output = stdout + stderr
    truncated = len(output) > 30000
    shown_stdout = stdout[-15000:] if truncated else stdout
    shown_stderr = stderr[-15000:] if truncated else stderr
    failure_kind, denial_observed = classify_shell_outcome(status, exit_code, applied, stderr)
    return json.dumps({
        "run_id": binding.run_id, "session_id": binding.session_id,
        "call_id": binding.call_id, "agent_id": binding.agent_id,
        "mode": str(binding.mode), "workspace": str(binding.workspace),
        "mode_source": binding.mode_source,
        "backend_applied": applied,
        "enforcement": "partial" if applied else "none",
        "status": status, "exit_code": exit_code,
        "failure_kind": failure_kind, "file_denial_observed": denial_observed,
        "stdout": shown_stdout, "stderr": shown_stderr,
        "output": output[-30000:] if truncated else output,
        "truncated": truncated, "approval_id": binding.approval_id,
    }, ensure_ascii=False)


@tool
def bash(
    command: str, runtime: ToolRuntime[dict],
    requested_mode: str | None = None, reason: str | None = None,
) -> str:
    """在工作区目录下执行 Bash 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "bash", ["-c"])


@tool
def sh(
    command: str, runtime: ToolRuntime[dict],
    requested_mode: str | None = None, reason: str | None = None,
) -> str:
    """在工作区目录下执行 POSIX sh 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "sh", ["-c"])


@tool
def cmd(
    command: str, runtime: ToolRuntime[dict],
    requested_mode: str | None = None, reason: str | None = None,
) -> str:
    """在工作区目录下执行 Windows CMD 命令；需要 host_command 权限。"""
    return _run_shell(command, runtime, "cmd.exe", ["/c"])


@tool
def powershell(
    command: str, runtime: ToolRuntime[dict],
    requested_mode: str | None = None, reason: str | None = None,
) -> str:
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
declare_all_effects([bash, powershell, cmd, sh], SANDBOXED_SHELL_EFFECT)
