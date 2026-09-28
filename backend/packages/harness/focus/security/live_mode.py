"""本文件对外提供 refreshed_security 和 refreshed_runtime_context，读取服务端当前会话模式。

输入为受治理 SecurityContext、运行上下文及其服务端异步模式读取器。
输出为本次新调用使用的安全上下文；已启动调用持有的绑定对象不被改写。
具体工作流为仅调用安全上下文 extras 内的读取器，按三档模式重建不可变授权身份，
并把机械投影写入新运行上下文；缺失读取器时沿用 Run 启动快照。
示例：context = await refreshed_runtime_context(runtime.context)；后续调用使用其 access_mode。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from focus.security.context import SecurityContext, has_security_context, security_context_of
from focus.security.policy import AccessMode


async def refreshed_security(security: SecurityContext) -> SecurityContext:
    resolver = security.extras.get("session_mode_resolver")
    if resolver is None:
        return security
    if not callable(resolver):
        raise RuntimeError("会话模式读取器无效")
    mode = await resolver()
    if not isinstance(mode, AccessMode):
        raise RuntimeError("会话模式读取器返回无效模式")
    return replace(
        security,
        authorization=replace(security.authorization, access_mode=mode),
        extras={**security.extras, "mode_source": "persistent-session"},
    )


async def refreshed_runtime_context(context: object) -> dict[str, Any]:
    if not has_security_context(context):
        return dict(context) if isinstance(context, Mapping) else {}
    security = await refreshed_security(security_context_of(context))
    values = context if isinstance(context, Mapping) else {}
    return {**values, **security.to_runtime_context()}
