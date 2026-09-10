"""plugins 根包入口。

插件目录名允许连字符(plugin.json 要求目录名 == name),不能直接作为 Python 包名;
本文件把连字符目录注册为下划线包路径,插件内部可用正规包路径互相 import。
子包 __init__ 逐个预加载自身模块并对缺失依赖容错(如 harness-only 环境没有 fastapi)。
新增带连字符的插件时在此按同样方式加一行。
"""

import importlib.util
import sys
from pathlib import Path

_REGISTRATIONS = (
    # (目录名, 包路径)
    ("spatial-patrol", "plugins.spatial_patrol"),
)

for _dir_name, _qualified in _REGISTRATIONS:
    if _qualified in sys.modules:
        continue
    _init_file = Path(__file__).parent / _dir_name / "__init__.py"
    if not _init_file.is_file():
        continue
    _spec = importlib.util.spec_from_file_location(_qualified, _init_file)
    if _spec is None or _spec.loader is None:
        continue
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_qualified] = _module
    _spec.loader.exec_module(_module)
    # 同时设置父包属性,保证 getattr/from import 与 sys.modules 一致
    _parent, _child = _qualified.rsplit(".", 1)
    if _parent in sys.modules:
        setattr(sys.modules[_parent], _child, _module)
