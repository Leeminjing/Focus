"""dsh-eyes 插件包入口。

目录名带连字符(plugin.json 要求目录名 == name),不能直接作为 Python 包名;
本文件按文件路径预加载子模块并注册到 sys.modules["plugins.dsh_eyes.*"],
使插件内部可以用正规包路径互相 import(与 loader 按路径加载的 entry 模块共享实例)。
"""

import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).parent
_SUBMODULES = ("config", "index", "eyes", "strip", "tool")

for _name in _SUBMODULES:
    _qualified = f"plugins.dsh_eyes.{_name}"
    if _qualified in sys.modules:
        continue
    _spec = importlib.util.spec_from_file_location(_qualified, _DIR / f"{_name}.py")
    if _spec is None or _spec.loader is None:
        continue
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_qualified] = _module
    try:
        _spec.loader.exec_module(_module)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "dsh-eyes 子模块 %s 加载失败(依赖缺失?),已跳过", _name, exc_info=True,
        )
        continue
    # 同时设置父包属性,保证 getattr/from import 与 sys.modules 一致
    _parent, _child = _qualified.rsplit(".", 1)
    if _parent in sys.modules:
        setattr(sys.modules[_parent], _child, _module)
