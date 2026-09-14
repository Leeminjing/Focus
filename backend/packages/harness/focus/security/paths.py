"""本文件对外提供真实宿主路径的规范化与归属判定，是路径解释的唯一归属地。

对外提供:
    canonical_text(path) — 把真实路径归一为可比较的文本形式
    is_within(root, target) — 判定目标是否位于某个根之内（含根自身）
    canonical_target(root, value) — 把工具收到的路径值解释为唯一的真实宿主路径

输入:
    path / root / target: Path — 真实宿主路径
    value: str — 工具收到的路径值，可以是绝对路径，也可以是相对根的值

输出:
    canonical_text → str — 归一后的可比较文本
    is_within → bool
    canonical_target → Path — 已解析的真实宿主路径（不校验存在性，也不判定是否允许访问）

具体工作流:
    (1) canonical_text 先剥掉 Windows 扩展路径前缀（`\\\\?\\` 与 `\\\\?\\UNC\\`），
        再做 normcase + normpath，使同一位置的两种写法可以比较
    (2) is_within 在归一后的文本上取 commonpath；跨盘符等不可比情形返回否
    (3) canonical_target 展开用户目录；绝对路径直接 resolve，相对值相对根 resolve
    (4) 本文件不回答「是否允许访问」——准入判定是另一处，两者不得合并

示例:
    canonical_target(Path("C:/ws"), "src/a.py")      # → C:\\ws\\src\\a.py
    is_within(Path("C:/ws"), Path("C:/ws/src"))      # → True
    is_within(Path("C:/ws"), Path("C:/other"))       # → False
"""

from __future__ import annotations

import os
from pathlib import Path

_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
_EXTENDED_PREFIX = "\\\\?\\"


def canonical_text(path: Path) -> str:
    """把真实路径归一为可比较文本：剥扩展前缀 + normcase + normpath。"""
    return os.path.normcase(os.path.normpath(_strip_extended_prefix(str(path))))


def is_within(root: Path, target: Path) -> bool:
    """判定目标是否位于某个根之内（含根自身）。"""
    root_text = canonical_text(root)
    target_text = canonical_text(target)
    try:
        return os.path.commonpath((root_text, target_text)) == root_text
    except ValueError:
        return False


def canonical_target(root: Path, value: str) -> Path:
    """把路径值解释为唯一的真实宿主路径；绝对路径直接用，相对值相对根解析。"""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _strip_extended_prefix(value: str) -> str:
    """剥掉 Windows 扩展路径前缀，使扩展写法与普通写法归一为同一文本。"""
    if os.name != "nt":
        return value
    if value.startswith(_EXTENDED_UNC_PREFIX):
        return "\\\\" + value[len(_EXTENDED_UNC_PREFIX) :]
    if value.startswith(_EXTENDED_PREFIX):
        return value[len(_EXTENDED_PREFIX) :]
    return value
