"""
本文件对外提供 build_server_params、build_servers_config 两个函数，负责将 McpServerConfig 翻译为
langchain-mcp-adapters 的 MultiServerMCPClient 可接受的 dict 格式。

对外提供:
    build_server_params(server_name, config): 单个 McpServerConfig → params dict
    build_servers_config(extensions_config): 所有启用的 servers → {name: params_dict} 映射

本模块为纯翻译层，不建立连接、不启动子进程、不持有任何有状态资源。

输入:
    build_server_params:
        server_name: str      — MCP server 名，用于异常信息与日志
        config: McpServerConfig — 单个 server 的配置对象

    build_servers_config:
        extensions_config: ExtensionsConfig — 全局扩展配置，可枚举所有 MCP servers

输出:
    build_server_params → dict，字段按 transport 类型不同
    build_servers_config → dict[str, dict]，失败的 server 跳过不抛

工作流:
    build_server_params:
    (1) 取 config.type，缺省 "stdio"，写入 params["transport"]
    (2) 按 transport 类型三分支:
        stdio: command 缺则 raise ValueError，填 command/args/env（非空才填）；
             env 值中的 $VAR / ${VAR} 引用在此（连接时）解析，缺失抛 ValueError
        sse / http: url 缺则 raise ValueError，填 url/headers（非空才填）
        其他类型 → raise ValueError
    (3) 返回 params

    build_servers_config:
    (1) extensions_config.get_enabled_mcp_servers() 拿启用列表
    (2) 空则 log info 返回 {}
    (3) 遍历每个 server，调 build_server_params，成功写入结果，异常 log error 跳过不抛
    (4) 返回结果 dict

示例:
    from focus.config import get_extensions_config
    from focus.mcp.client import build_servers_config

    cfg = get_extensions_config("extensions_config.json")
    servers = build_servers_config(cfg)
    # → {"filesystem": {"transport": "stdio", "command": "npx", ...}}
"""

import logging
import os

from focus.config.env import match_env_ref
from focus.config.extensions_config import ExtensionsConfig, McpServerConfig

logger = logging.getLogger(__name__)

_SUPPORTED_TRANSPORTS = frozenset({"stdio", "sse", "http"})


def _resolve_env_value(value: str, server_name: str) -> str:
    """连接时解析 $VAR / ${VAR} 环境变量引用；缺失抛 ValueError（仅 enabled server 到达此处）。"""
    var_name = match_env_ref(value)
    if var_name is None:
        return value
    env_value = os.environ.get(var_name)
    if env_value is None:
        raise ValueError(
            f"MCP server '{server_name}': 环境变量未设置: {var_name}"
        )
    return env_value


def build_server_params(server_name: str, config: McpServerConfig) -> dict:
    transport = config.type or "stdio"
    if transport not in _SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"MCP server '{server_name}': 不支持的 transport 类型 '{transport}'，"
            f"仅支持 {sorted(_SUPPORTED_TRANSPORTS)}"
        )

    params: dict = {"transport": transport}

    if transport == "stdio":
        if not config.command:
            raise ValueError(
                f"MCP server '{server_name}': stdio 类型必须提供 command 字段"
            )
        params["command"] = config.command
        if config.args:
            params["args"] = config.args
        if config.env:
            params["env"] = {
                key: _resolve_env_value(value, server_name)
                for key, value in config.env.items()
            }
    else:
        if not config.url:
            raise ValueError(
                f"MCP server '{server_name}': sse/http 类型必须提供 url 字段"
            )
        params["url"] = config.url
        if config.headers:
            params["headers"] = config.headers

    return params


def build_servers_config(extensions_config: ExtensionsConfig) -> dict[str, dict]:
    enabled = extensions_config.get_enabled_mcp_servers()
    if not enabled:
        logger.info("没有启用的 MCP Server")
        return {}

    result: dict[str, dict] = {}
    for name, config in enabled.items():
        try:
            result[name] = build_server_params(name, config)
            logger.info("MCP Server '%s' 配置翻译成功 (transport=%s)", name, config.type or "stdio")
        except Exception:
            logger.error("MCP Server '%s' 配置翻译失败，已跳过", name, exc_info=True)

    return result
