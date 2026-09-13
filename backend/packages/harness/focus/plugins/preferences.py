"""本文件对外提供插件启停偏好的读写，是「用户停用了哪些插件」这一状态的唯一归属地。

对外提供:
    disabled_plugins() — 读取当前被用户停用的插件名集合
    set_plugin_disabled(name, disabled) — 写入单个插件的启停偏好，返回写入后的集合
    preference_path() — 偏好文件路径（供调试与测试定位）

输入:
    name: str — 插件名（与 plugin.json 的 name 及目录名一致）
    disabled: bool — True 表示停用，False 表示启用

输出:
    disabled_plugins → set[str]；set_plugin_disabled → set[str]（写入后的完整集合）

具体工作流:
    (1) 偏好存放在全局家目录，与仓库内置插件目录分离——用户开关不写入、也不污染代码仓库，
        因此升级覆盖仓库文件时用户选择仍然保留
    (2) 读取缺失、损坏或结构不符一律按「无停用项」处理，绝不因偏好文件异常而阻断插件加载
    (3) 写入采用「先写临时文件再原子替换」，避免写入中断留下半截 JSON
    (4) 清单里的 enabled=false 由开发者随插件发布，本偏好只表达用户选择；两者都生效且互不覆盖
        ——因此用户无法通过本偏好启用一个发布时即关闭的插件

示例:
    set_plugin_disabled("spatial-patrol", True)   # 用户停用
    set_plugin_disabled("spatial-patrol", False)  # 用户重新启用
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from focus.config.layered import global_home

PREFERENCE_FILENAME = "plugins-disabled.json"


def preference_path() -> Path:
    return global_home() / PREFERENCE_FILENAME


def disabled_plugins() -> set[str]:
    path = preference_path()
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names = raw.get("disabled") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return set()
    return {str(name) for name in names if isinstance(name, str) and name}


def set_plugin_disabled(name: str, disabled: bool) -> set[str]:
    names = disabled_plugins()
    if disabled:
        names.add(name)
    else:
        names.discard(name)
    _write(names)
    return names


def _write(names: set[str]) -> None:
    path = preference_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"disabled": sorted(names)}, ensure_ascii=False, indent=2) + "\n"
    # 同目录临时文件 + 原子替换：写入中断不会留下半截 JSON
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
