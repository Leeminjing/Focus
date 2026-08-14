"""环境变量引用解析的唯一入口（change 13）。

约定:只处理 $VAR / ${VAR} 形式的完整值;是否抛错由调用方按其场景决定
（配置加载期硬失败 KeyError / MCP 连接期校验 ValueError / 加载期软返回）。
"""

import os
import re

_ENV_VAR_PATTERN = re.compile(r"^\$([A-Z_][A-Z0-9_]*)$|^\$\{([A-Z_][A-Z0-9_]*)\}$")


def match_env_ref(value: object) -> str | None:
    """正则命中 $VAR / ${VAR} 时返回变量名;非引用返回 None。"""
    if not isinstance(value, str):
        return None
    match = _ENV_VAR_PATTERN.match(value)
    if not match:
        return None
    return match.group(1) or match.group(2)


def resolve_env_var(value: object) -> str | None:
    """解析 $VAR / ${VAR} 引用;非引用或环境变量未命中返回 None。"""
    var_name = match_env_ref(value)
    if var_name is None:
        return None
    return os.environ.get(var_name)
