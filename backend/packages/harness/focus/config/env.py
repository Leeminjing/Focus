"""环境变量引用解析的唯一入口。

本模块同时收拢「机制」与「策略」，对外形成两阶段契约:

    (1) 加载期 resolve_env_vars —— 递归软解析。
        只有 match_env_ref 命中的合法 $VAR / ${VAR} 才是引用；解析不到时原样保留该
        字面量，永不抛错。因此「某项可选能力的密钥缺失」不会阻塞配置加载，也不会
        被误判为必需项。
    (2) 使用期 require_env_var —— 在真正消费该值的唯一位置显式解析。
        值不是引用则原样返回；是引用但变量未设置时，抛出带 context 的 ValueError，
        指出缺哪个变量、谁需要它。

失败类型选用 ValueError 而非 KeyError: 插件装配链（如 spatial-patrol 的 vision）
以 ValueError 判定「依赖缺失」，据此把插件标记为不可用，从而完成降级而不是崩溃。

对外提供:
    match_env_ref(value) — 命中 $VAR / ${VAR} 时返回变量名；非引用返回 None
    resolve_env_var(value) — 解析单个引用；非引用或变量未命中返回 None
    resolve_env_vars(data) — 加载期递归软解析 dict / list / 标量
    require_env_var(value, *, context) — 使用期解析；缺失时抛带上下文的 ValueError

示例:
    raw = resolve_env_vars({"a": "$SET_ME", "b": "$100"})   # 缺失的引用保留原值
    key = require_env_var("$OPENAI_API_KEY", context="模型 'demo'")
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


def resolve_env_vars(data):
    """加载期递归软解析:解析得到的引用替换为值,未命中的引用原样保留,永不抛错。

    只有 match_env_ref 命中的合法引用才是候选;诸如 `$100` 这类以 $ 开头但并非引用的
    字面量按普通值保留。
    """
    if isinstance(data, dict):
        return {key: resolve_env_vars(value) for key, value in data.items()}
    if isinstance(data, list):
        return [resolve_env_vars(item) for item in data]
    resolved = resolve_env_var(data)
    return data if resolved is None else resolved


def require_env_var(value, *, context: str):
    """使用期解析:非引用原样返回;引用但变量未设置时抛带 context 的 ValueError。

    context 描述「谁需要这个值」（如模型名、MCP server 名），使失败信息可定位。
    """
    var_name = match_env_ref(value)
    if var_name is None:
        return value
    resolved = os.environ.get(var_name)
    if resolved is None:
        raise ValueError(f"环境变量未设置: {var_name}（{context} 需要它）")
    return resolved
