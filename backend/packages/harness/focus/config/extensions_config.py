"""
本文件定义 McpServerConfig 与 ExtensionsConfig 两个 Pydantic 配置模型，以及 get_extensions_config 加载函数。

对外提供:
    McpServerConfig(BaseModel): 单个 MCP Server 的配置对象
    ExtensionsConfig(BaseModel): extensions_config.json 的顶层模型，聚合所有 MCP Server 配置
    get_extensions_config(json_path): 加载 extensions_config.json 并返回 ExtensionsConfig 实例
    get_enabled_mcp_servers(config): 从 ExtensionsConfig 中筛选 enabled=true 的 MCP Server

输入:
    McpServerConfig 字段:
        enabled: bool              — 是否启用该 server
        type: str                  — transport 类型，取值 stdio / sse / http
        description: str           — 给人看的说明
        command: str | None        — 仅 stdio，可执行文件路径
        args: list[str] | None     — 仅 stdio，命令行参数
        env: dict[str, str] | None — 仅 stdio，注入子进程的环境变量
        url: str | None            — 仅 sse/http，远程端点地址
        headers: dict[str, str] | None — 仅 sse/http，HTTP 请求头

输出:
    get_extensions_config → ExtensionsConfig 实例，含递归解析后的环境变量

工作流:
    get_extensions_config:
    (1) 定位 json 文件路径
    (2) 读取为 dict
    (3) 递归解析 dict 中所有值为 $VAR / ${VAR} 格式的环境变量引用
    (4) 调用 ExtensionsConfig.model_validate() 生成实例

    get_enabled_mcp_servers:
    (1) 遍历 mcp_servers 字典
    (2) 筛选 enabled == True 的条目
    (3) 返回 dict[str, McpServerConfig]

示例:
    config = get_extensions_config("extensions_config.json")
    enabled_servers = config.get_enabled_mcp_servers()
    for name, cfg in enabled_servers.items():
        print(f"{name}: {cfg.type}")
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from focus.config.env import resolve_env_var


class McpServerConfig(BaseModel):
    enabled: bool
    type: str
    description: str
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None


class ExtensionsConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(alias="mcpServers")

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        return {
            name: cfg
            for name, cfg in self.mcp_servers.items()
            if cfg.enabled
        }


def get_extensions_config(json_path: str) -> ExtensionsConfig:
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"扩展配置文件不存在: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    resolved = _resolve_env_vars(raw)
    return ExtensionsConfig.model_validate(resolved)


def get_enabled_mcp_servers(config: ExtensionsConfig) -> dict[str, McpServerConfig]:
    return config.get_enabled_mcp_servers()


def _resolve_env_item(value):
    # 命中 $VAR 但环境变量缺失时 resolve_env_var 返回 None,此处软返回原值——
    # 加载阶段不抛错(disabled server 的 $VAR 不参与解析),缺失校验推迟到连接时
    return resolve_env_var(value) or value


def _resolve_env_vars(data):
    if isinstance(data, dict):
        return {key: _resolve_env_vars(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_resolve_env_vars(item) for item in data]
    return _resolve_env_item(data)
