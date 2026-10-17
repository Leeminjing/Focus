"""配置转换和连接共用的 MCP 失败诊断，传递至桌面刷新结果。"""

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class McpFailure:
    server_name: str
    scope: str
    config_path: str | None
    stage: str
    reason: str
    action: str


class McpConfigurationError(ValueError):
    def __init__(self, reason: str, action: str):
        super().__init__(reason)
        self.action = action


def server_failure(
    name: str, sources: dict[str, dict[str, str]], *, stage: str, reason: str, action: str,
) -> McpFailure:
    source = sources.get(name, {})
    return McpFailure(name, source.get("scope", "provided"), source.get("config_path"), stage, reason, action)


def connection_reason(exc: Exception, params: dict) -> str:
    """展开任务组异常，并移除连接参数中的密钥和 URL 凭据。"""
    def messages(error):
        if isinstance(error, BaseExceptionGroup):
            return [message for child in error.exceptions for message in messages(child)]
        return [f"{type(error).__name__}: {error}"]

    reason = "; ".join(dict.fromkeys(messages(exc)))
    secrets = [*params.get("env", {}).values(), *params.get("headers", {}).values()]
    # 库可能输出完整 URL 或请求头，也可能只输出 token 本身。
    url = params.get("url")
    if url:
        secrets.append(url)
        try:
            parts = urlsplit(url)
            secrets.extend([parts.username, parts.password, *(value for _, value in parse_qsl(parts.query))])
        except ValueError:
            pass  # 非法 URL 本身也必须能生成失败诊断。
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        reason = reason.replace(secret, "[redacted]")
        if secret.lower().startswith("bearer "):
            reason = reason.replace(secret[7:], "[redacted]")
    reason = re.sub(r"https?://[^\s'\"<>]+", "[url]", reason)
    return reason[:1000]
