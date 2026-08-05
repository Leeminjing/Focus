"""
本文件对外提供 `CSRFMiddleware` FastAPI 中间件，实现 Cookie/Header 双提交 CSRF 防护。

对外提供:
    CSRFMiddleware(BaseHTTPMiddleware) — CSRF 双提交校验中间件

输入:
    __init__: auth_config: AuthConfig — 认证配置

输出:
    dispatch 中校验 Cookie csrf_token 与 Header X-CSRF-Token 一致性；不一致返回 403

具体工作流:
    (1) 检查请求方法：GET/HEAD/OPTIONS → 直接放行
    (2) 检查请求路径：以 /api/auth/ 开头 → 放行（登录/登出不校验 CSRF）
    (3) 从 request.cookies 读取 csrf_cookie_name
    (4) 从 request.headers 读取 X-CSRF-Token
    (5) 比对两者一致且非空 → 放行
    (6) 不一致或缺失 → 返回 403 JSONResponse

示例:
    from backend.app.gateway.middleware.csrf import CSRFMiddleware
    app.add_middleware(CSRFMiddleware, auth_config=auth_config)
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.app.gateway.auth.config import AuthConfig

logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_SKIP_PREFIXES = ("/api/auth/login", "/api/auth/logout", "/desktop/")


class CSRFMiddleware(BaseHTTPMiddleware):
    """Cookie/Header 双提交 CSRF 防护中间件。

    输入:
        app — ASGI application
        auth_config: AuthConfig — 认证配置

    输出:
        dispatch 中校验 CSRF token 一致性后放行或返回 403
    """

    def __init__(self, app, auth_config: AuthConfig):
        super().__init__(app)
        self._csrf_cookie_name = auth_config.csrf_cookie_name

    async def dispatch(self, request: Request, call_next):
        # (1) 安全方法跳过
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        # (2) 认证路由跳过（login/logout 不需要 CSRF）；桌面路由由会话密钥
        #     与 loopback 对等地址校验保护，跳过 CSRF 双提交
        if any(request.url.path.startswith(prefix) for prefix in _CSRF_SKIP_PREFIXES):
            return await call_next(request)

        # (3) 读取 Cookie 和 Header 中的 CSRF token
        cookie_token = request.cookies.get(self._csrf_cookie_name)
        header_token = request.headers.get("X-CSRF-Token")

        # (4) 双提交校验
        if cookie_token is None or header_token is None:
            logger.debug("CSRF token 缺失: path=%s, method=%s", request.url.path, request.method)
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token 缺失"},
            )

        if cookie_token != header_token:
            logger.debug("CSRF token 不匹配: path=%s, method=%s", request.url.path, request.method)
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token 不匹配"},
            )

        return await call_next(request)
