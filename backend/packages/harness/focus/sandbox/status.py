"""本文件对外提供 sandbox_status，返回 Windows 文件沙箱可用性、持久授权与实际边界。

输入为本机平台、Focus Node 原生模块安装及工作区准备尝试登记；输出为可展示的只读状态数据。
具体工作流为检查执行后端存在性，读取常驻授权清单并列出必须向用户说明的限制；
本函数不创建令牌、不修改工作区，也不把完全访问误报为受限执行。
示例：status = sandbox_status()；status["enforcement"] 为 "partial" 或 "unavailable"。
"""

import os
import shutil

from focus.sandbox.prepared import PreparedWorkspaceRegistry
from focus.sandbox.windows import WindowsAclBackend


_LIMITATIONS = (
    "不保证读取保密、网络隔离、进程隐藏或独立控制台；并非完整虚拟机隔离或独立 Windows 用户身份。",
    "NTFS 硬链接可能使工作区内外路径指向同一文件对象，不保证路径级绝对隔离。",
    "CIM/WMI、部分 AppContainer ACL 目录和子进程管道捕获可能不兼容。",
    "清单包括已准备或曾尝试准备的工作区；Low 标记与目录 ACL 调整可能持久保留，并影响其他低完整性进程。",
    "结构化路径检查存在检查到使用的竞态；极端外部强杀可能留下未执行的挂起进程。",
)


def sandbox_status() -> dict[str, object]:
    available = (
        os.name == "nt"
        and shutil.which("node") is not None
        and WindowsAclBackend._default_runner().is_file()
        and WindowsAclBackend._default_native_module().is_file()
        and WindowsAclBackend._default_koffi_package().is_file()
        and WindowsAclBackend._default_grant_helper().is_file()
    )
    return {
        "available": available,
        "enforcement": "partial" if available else "unavailable",
        "limited_label": "已启用，部分约束" if available else "受限后端不可用",
        "unrestricted_label": "未应用文件沙箱",
        "prepared_workspaces": list(PreparedWorkspaceRegistry().list()),
        "limitations": list(_LIMITATIONS),
    }
