"""
本文件对外提供 AuthMiddleware FastAPI 中间件，作为全接口统一会话保护入口。

对外提供:
    AuthMiddleware(BaseHTTPMiddleware) — loopback 对等地址 + X-Focus-Session 会话密钥保护

输入:
    __init__: app — ASGI application（无配置参数，会话密钥运行时读 app.state.session_key）

输出:
    dispatch 中校验通过后放行；非 loopback 返回 404，会话密钥缺失/不匹配返回 401

具体工作流:
    (1) 保护面：/api/threads/* 与 /desktop/api/*（统一运行接口与桌面业务接口）
    (2) 其他路径（/desktop/ 静态资源、/health、/docs）直接放行
    (3) 检查请求对等地址是否为 loopback（127.0.0.1/::1），否则 404（桌面路由不暴露到网络）
    (4) 校验 X-Focus-Session 请求头或 session 查询参数与 app.state.session_key 一致，否则 401
    (5) 通过后放行到路由

示例:
    app.add_middleware(AuthMiddleware)
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# 统一保护面：/api/threads/*（运行接口）与 /desktop/api/*（桌面业务接口）
_PROTECTED_PREFIXES = ("/api/threads", "/desktop/api")


class AuthMiddleware(BaseHTTPMiddleware):
    """统一桌面会话保护：loopback 对等地址 + 会话密钥（替代 JWT/Cookie 认证）。"""

    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # (1) 保护面判定
        if not any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
            return await call_next(request)

        # (2) loopback 对等地址校验
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # (3) 会话密钥校验
        supplied = request.headers.get("X-Focus-Session") or request.query_params.get("session")
        session_key = getattr(request.app.state, "session_key", None)
        if not session_key or supplied != session_key:
            return JSONResponse(status_code=401, content={"detail": "无效的桌面会话"})

        # (4) 放行
        return await call_next(request)
