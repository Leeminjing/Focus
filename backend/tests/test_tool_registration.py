"""用户自定义 tool 分离（RegisterTool / ToolRegistry / get_available_tools）的单元测试。"""

import asyncio
import shutil
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from focus.tools.interfaces import RegisterTool, ToolInfo
from focus.tools.registry import ToolRegistry


def _install_custom_tool(root: Path, name: str = "greet") -> Path:
    """在 root 下放置一个导出 @tool 的自定义工具目录。"""
    tool_dir = root / name
    tool_dir.mkdir(parents=True, exist_ok=True)
    entry = tool_dir / "tool.py"
    entry.write_text(
        "from langchain_core.tools import tool\n"
        "@tool\n"
        f"def {name}(name: str) -> str:\n"
        "    '''Say hello.'''\n"
        "    return f'hello {name}'\n\n"
        f"tool = {name}\n",
        encoding="utf-8",
    )
    return entry


def _tempdir():
    """沙箱可写的临时目录。

    Windows 下 tempfile.mkdtemp 生成的随机目录再建子目录会触发 PermissionError(5)，
    改用工作区 tmp/ 下的固定名称目录（已验证可写）。
    """
    workspace_tmp = Path(__file__).resolve().parents[2] / "tmp"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    root = workspace_tmp / "_focus_tool_test"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_register_tool_is_protocol():
    """RegisterTool 是 runtime_checkable 的结构协议，实现 build_tool() 即视为 tool。"""
    assert isinstance(RegisterTool, type)

    class Dummy:
        def build_tool(self):
            return tool(lambda: "ok")("ignored")

    assert isinstance(Dummy(), RegisterTool)


def test_tool_info_fields_complete():
    """ToolInfo 载体字段齐全。"""
    assert set(ToolInfo.__dataclass_fields__.keys()) == {
        "name", "label", "description", "source", "build_tool", "prompt_snippet",
    }


def test_tool_registry_scans_custom_tool():
    """ToolRegistry 能扫描自定义工具并可构建 BaseTool。"""
    root = _tempdir()
    try:
        _install_custom_tool(root)
        infos = ToolRegistry(root=root).tools()
        assert len(infos) == 1
        info = infos[0]
        assert info.source == "custom"
        assert info.name == "greet"
        assert isinstance(info.tool(), BaseTool)
        assert isinstance(info, ToolInfo)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_get_available_tools_returns_toolinfo(monkeypatch):
    """get_available_tools 聚合全部来源并返回带 source 的 ToolInfo[]。"""
    root = _tempdir()
    try:
        monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(root))
        _install_custom_tool(root / "tools", name="hello")
        from focus.tools.tools import get_available_tools

        infos = asyncio.run(get_available_tools())
        sources = {info.source for info in infos}
        # builtin 与 custom 必在；mcp/plugin 可能因环境依赖缺失而空，不强断言
        assert all(isinstance(info, ToolInfo) for info in infos)
        assert "builtin" in sources
        assert "custom" in sources
        for info in infos:
            assert isinstance(info.tool(), BaseTool)
    finally:
        shutil.rmtree(root, ignore_errors=True)
