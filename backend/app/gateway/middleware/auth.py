"""
本文件对外提供 `AuthMiddleware` FastAPI 中间件，作为主鉴权入口。

对外提供:
    AuthMiddleware(BaseHTTPMiddleware) — JWT Cookie 认证中间件

输入:
    __init__: auth_config: AuthConfig — 认证配置

输出:
    dispatch 中将 user 对象注入 request.state.current_user 后放行；认证失败返回 401

具体工作流:
    (1) 检查请求路径是否在白名单（/api/auth/、/docs、/openapi.json、/redoc）
    (2) 白名单 → 直接放行
    (3) 从 request.cookies 读取 access_token
    (4) 调用 decode_access_token 解码 JWT
    (5) 从 DB 查 User，比对 payload.ver == user.token_version
    (6) request.state.current_user = user → 调用 self.app() 放行
    (7) 异常 → 返回 401 JSONResponse

示例:
    from backend.app.gateway.middleware.auth import AuthMiddleware
    app.add_middleware(AuthMiddleware, auth_config=auth_config)
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.app.gateway.auth.config import AuthConfig
from backend.app.gateway.auth.security import decode_access_token
from focus.persistence.engine import get_session
from backend.app.gateway.models.user import User
from sqlalchemy import select

logger = logging.getLogger(__name__)

_AUTH_WHITELIST_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    # Electron 启动链的健康检查（不带 JWT cookie）
    "/health",
}
_AUTH_WHITELIST_PREFIXES = [
    "/docs",
    "/openapi.json",
    "/redoc",
    # 桌面路由与静态资源：不走 JWT/CSRF，由 require_desktop_session 的
    # 会话密钥 + loopback 对等地址校验保护（设计决策 1、10）
    "/desktop/",
]


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT Cookie 认证中间件，在请求进入路由前校验用户身份。

    输入:
        app — ASGI application
        auth_config: AuthConfig — 认证配置

    输出:
        dispatch(request, call_next) 中将 user 注入 request.state.current_user
    """

    def __init__(self, app, auth_config: AuthConfig):
        super().__init__(app)
        self._auth_config = auth_config

    async def dispatch(self, request: Request, call_next):
        # (1) 白名单路径放行
        path = request.url.path
        if path in _AUTH_WHITELIST_PATHS:
            return await call_next(request)
        for prefix in _AUTH_WHITELIST_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # (2) 从 Cookie 读取 token
        token = request.cookies.get(self._auth_config.cookie_name)
        if token is None:
            logger.debug("未提供 access_token Cookie: path=%s", path)
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录"},
            )

        # (3) 解码 JWT
        try:
            payload = decode_access_token(token, self._auth_config)
        except Exception:
            logger.debug("JWT 解码失败: path=%s", path, exc_info=True)
            return JSONResponse(
                status_code=401,
                content={"detail": "登录已过期，请重新登录"},
            )

        # (4) 查 DB 验证 token_version
        user_id = payload.get("sub")
        token_ver = payload.get("ver")
        if user_id is None or token_ver is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "无效的 token"},
            )

        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()

        if user is None:
            logger.debug("User 不存在: user_id=%s", user_id)
            return JSONResponse(
                status_code=401,
                content={"detail": "用户不存在"},
            )

        if user.token_version != token_ver:
            logger.debug("token_version 不匹配: user_id=%s, token_ver=%s, db_ver=%s",
                         user_id, token_ver, user.token_version)
            return JSONResponse(
                status_code=401,
                content={"detail": "登录已失效，请重新登录"},
            )

        # (5) 注入用户身份
        request.state.current_user = user
        return await call_next(request)
