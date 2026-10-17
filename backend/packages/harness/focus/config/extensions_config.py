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
        trust: dict[str, str] | None — 用户显式信任覆盖：工具名（`*` 为全部）→ 取值名；
            取值域窄于效果分类，只允许 no_local_effect / opaque_local，越界取值被拒并保持最严

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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from focus.config.env import resolve_env_vars


class McpServerConfig(BaseModel):
    enabled: bool
    type: str
    description: str
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    trust: dict[str, str] | None = None


class ExtensionsConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(alias="mcpServers")
    # 由加载器生成，不作为用户配置序列化；缓存单独纳入来源，避免跨作用域误报。
    mcp_server_sources: dict[str, dict[str, str]] = Field(default_factory=dict, exclude=True)
    mcp_server_errors: dict[str, str] = Field(default_factory=dict, exclude=True)

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        return {
            name: cfg
            for name, cfg in self.mcp_servers.items()
            if cfg.enabled
        }


def get_extensions_config(json_path: str, *, workspace: str | Path | None = None) -> ExtensionsConfig:
    """读取程序默认 < 全局 < 工作区扩展配置，并解析环境变量。"""
    from focus.config.layered import load_layered_map_with_sources

    raw, sources = load_layered_map_with_sources(Path(json_path).name, json_path, workspace=workspace)
    resolved = resolve_env_vars(raw)
    if not resolved.get("mcpServers"):
        return ExtensionsConfig(mcp_servers={})
    servers = {}
    errors = {}
    for name, entry in resolved["mcpServers"].items():
        try:
            servers[name] = McpServerConfig.model_validate(entry)
        except ValidationError as exc:
            if isinstance(entry, dict) and entry.get("enabled") is False:
                continue
            # 不序列化 input，避免把 env / headers 等字段的密钥带入刷新响应。
            errors[name] = "配置字段无效：" + "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'server'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
    return ExtensionsConfig(
        mcp_servers=servers, mcp_server_sources=sources.get("mcpServers", {}), mcp_server_errors=errors,
    )


def get_enabled_mcp_servers(config: ExtensionsConfig) -> dict[str, McpServerConfig]:
    return config.get_enabled_mcp_servers()
