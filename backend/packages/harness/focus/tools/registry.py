"""
本文件对外提供 ToolRegistry 类，作为用户自定义 tool 的目录扫描与构建入口。

对外提供:
    ToolRegistry — 从全局目录 ~/.focus/tools/<tool-name>/ 扫描并构建用户自定义工具
    get_tool_registry() — 懒加载单例（与 get_plugin_registry / get_mcp_tools_cached 同模式）
    reload_tool_registry() — 重建注册表（会话内加载新自定义工具）

输入:
    ToolRegistry(root) — 用户自定义工具根目录（默认 global_home()/tools，即 ~/.focus/tools）

输出:
    list[ToolInfo] — 扫描到的用户自定义工具（source="custom"），每个 ToolInfo.build_tool() 返回 BaseTool

具体工作流:
    (1) 遍历 root 下的每个子目录（目录名即 tool name），跳过无入口的目录
    (2) 用 importlib.spec_from_file_location + 独立模块名加载入口文件（不污染 sys.modules，
        与 focus/plugins/loader.py 同模式）
    (3) 从模块中提取符合 RegisterTool 的工具对象或 build_tool() 工厂，构建 BaseTool
    (4) 包装为 ToolInfo(source="custom")，name=目录名，label/description 取自工具元数据或回退
    (5) 单例按 root 缓存；get_tool_registry 返回缓存，reload_tool_registry 重建

示例:
    registry = get_tool_registry()
    tools = registry.tools()  # -> list[ToolInfo]（source="custom"）
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from focus.config.layered import global_home
from focus.tools.interfaces import RegisterTool, ToolInfo

logger = logging.getLogger(__name__)

# 用户自定义工具入口文件名（每目录一个，暴露 tool 对象或 build_tool 工厂）
_ENTRY_FILENAME = "tool.py"
# 从目录派生独立模块名的前缀，避免撞 sys.modules
_MODULE_PREFIX = "focus_user_tool_"

_registries: dict[str, "ToolRegistry"] = {}


def _tool_module_name(name: str) -> str:
    return f"{_MODULE_PREFIX}{name}"


class ToolRegistry:
    """用户自定义工具的目录扫描与构建器（source="custom"）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else (global_home() / "tools")
        self._tools: list[ToolInfo] = []
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.root.is_dir():
            return
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name.casefold()):
            if not entry.is_dir():
                continue
            tool = self._build_tool_from_dir(entry)
            if tool is None:
                continue
            self._tools.append(tool)

    def _build_tool_from_dir(self, tool_dir: Path) -> ToolInfo | None:
        """从单个工具目录构建 ToolInfo；失败返回 None 并记录日志。"""
        entry_file = tool_dir / _ENTRY_FILENAME
        if not entry_file.is_file():
            logger.warning("用户工具目录缺少 %s: %s", _ENTRY_FILENAME, tool_dir)
            return None
        module = self._load_module(tool_dir.name, entry_file)
        if module is None:
            return None
        tool = self._resolve_tool(module, tool_dir.name)
        if tool is None:
            logger.warning("用户工具未导出有效 RegisterTool 或 build_tool: %s", tool_dir)
            return None
        name = getattr(tool, "name", None) or tool_dir.name
        label = getattr(tool, "label", None) or tool_dir.name
        description = getattr(tool, "description", None) or ""
        return ToolInfo(
            name=name,
            label=label,
            description=description,
            source="custom",
            build_tool=lambda t=tool: t,
            prompt_snippet=None,
        )

    @staticmethod
    def _load_module(name: str, entry_file: Path) -> Any | None:
        spec = importlib.util.spec_from_file_location(_tool_module_name(name), entry_file)
        if spec is None or spec.loader is None:
            logger.error("用户工具入口无法加载: %s", entry_file)
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.error("用户工具入口执行失败: %s", entry_file, exc_info=True)
            return None
        return module

    @staticmethod
    def _resolve_tool(module: Any, name: str) -> BaseTool | None:
        """从入口模块提取 BaseTool：依次尝试 module.tool（RegisterTool 或 BaseTool）、module.build_tool 工厂。"""
        candidate = getattr(module, "tool", None)
        if isinstance(candidate, BaseTool):
            return candidate
        if isinstance(candidate, RegisterTool):
            return candidate.build_tool()
        build_tool = getattr(module, "build_tool", None)
        if callable(build_tool):
            try:
                return build_tool()
            except TypeError:
                try:
                    return build_tool(module)
                except Exception:
                    logger.error("build_tool 调用失败: %s", name, exc_info=True)
                    return None
        return None

    def tools(self) -> list[ToolInfo]:
        self._load()
        return list(self._tools)


def get_tool_registry(root: str | Path | None = None) -> ToolRegistry:
    """返回指定根目录的用户自定义工具注册表单例（同一 root 多次调用返回同一实例）。"""
    key = str(root) if root is not None else "default"
    if key not in _registries:
        _registries[key] = ToolRegistry(root)
    return _registries[key]


def reload_tool_registry(root: str | Path | None = None) -> ToolRegistry:
    """重建指定根目录的工具注册表并替换缓存（会话内加载新自定义工具）。

    正在运行的 run 使用装配时快照，不受影响；下一次 run 装配时读到新注册表。
    """
    key = str(root) if root is not None else "default"
    registry = ToolRegistry(root)
    _registries[key] = registry
    return registry
