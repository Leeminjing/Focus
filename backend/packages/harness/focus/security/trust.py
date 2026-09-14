"""本文件是外部执行工具（MCP / 进程外第三方）信任分层的唯一归属地，回答「谁有权说这个工具没有本地副作用」。

对外提供:
    TRUST_VALUES — 用户显式覆盖允许的取值（只能在不透明与无本地效果之间选择）
    SHIPPED_CONTRACTS — 随版本提供的契约注册表，键为 server 名，值为工具名到契约的映射
    shipped_contract(server, tool) — 查询随版本契约
    parse_trust_override(server, raw) — 解析并校验用户显式覆盖，越界取值被拒
    trust_tools(server, tools, override) — 摄取边界：先清除自声明，再按三层签发

输入:
    server: str — server 名（用户扩展配置里的键，或本系统自接 server 的固定名）
    tool: str — 工具名；`*` 表示该 server 下的全部工具
    raw: object — 用户扩展配置里的信任段落（工具名到取值名的映射）
    override: Mapping[str, EffectContract] — parse_trust_override 的结果

输出:
    shipped_contract → EffectContract | None；未注册即 None
    parse_trust_override → dict[str, EffectContract]；越界取值被丢弃并留下告警
    trust_tools → list[BaseTool]，同一顺序；外来自声明一律不参与判定

具体工作流:
    (1) 清除工具自带的契约槽：外部执行工具的自声明只作提示，不参与任何决策
    (2) 命中随版本契约即签发之；本系统自行接线的 server 在此注册，与用户配置无关，
        因此信任策略的作用域覆盖自接 server，而不只是用户扩展配置里的 server
    (3) 否则命中用户显式覆盖即按覆盖值签发；覆盖的取值域窄于效果分类，
        结构性放宽（可结构化枚举 / 创建执行主体）需要 Focus 侧存在解析器或受管执行主体，用户无法凭空声明
    (4) 都没有命中则不签发，读取侧一律按不透明，即工作区保护下逐次批准

示例:
    tools = trust_tools("context7", loaded, {})            # → 随版本契约生效
    tools = trust_tools("user-server", loaded, {"*": NO_LOCAL_EFFECT})
    trust_tools("unknown", loaded, {})[0]                  # → 未签发，读取侧按不透明
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from langchain_core.tools import BaseTool

from focus.security.effects import (
    NO_LOCAL_EFFECT,
    OPAQUE_LOCAL_EFFECT,
    EffectContract,
    ToolEffectKind,
    declare_effect,
    discard_declared_effect,
)

logger = logging.getLogger(__name__)

TRUST_VALUES: Mapping[str, EffectContract] = {
    ToolEffectKind.NO_LOCAL_EFFECT.value: NO_LOCAL_EFFECT,
    ToolEffectKind.OPAQUE_LOCAL.value: OPAQUE_LOCAL_EFFECT,
}

CONTEXT7_SERVER = "context7"

SHIPPED_CONTRACTS: Mapping[str, Mapping[str, EffectContract]] = {
    CONTEXT7_SERVER: {
        "resolve-library-id": NO_LOCAL_EFFECT,
        "query-docs": NO_LOCAL_EFFECT,
    },
}


def shipped_contract(server: str, tool: str) -> EffectContract | None:
    """查询随版本契约；未注册返回 None，由调用方继续回落。"""
    return SHIPPED_CONTRACTS.get(server, {}).get(tool)


def parse_trust_override(server: str, raw: object) -> dict[str, EffectContract]:
    """把用户扩展配置里的信任段落解析为已签发的覆盖值；越界取值被拒。

    取值域窄于效果分类：只允许在不透明与无本地效果之间选择。可结构化枚举需要
    Focus 侧存在目标解析器，创建执行主体需要 Focus 侧存在受管执行主体，两者都
    不可由用户声明，因此被拒绝并保持最严。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        logger.warning("MCP server '%s' 的信任覆盖不是映射，已忽略并保持最严", server)
        return {}

    overrides: dict[str, EffectContract] = {}
    for tool_name, value in raw.items():
        contract = TRUST_VALUES.get(str(value))
        if contract is None:
            logger.warning(
                "MCP server '%s' 的信任覆盖 %r → %r 越出允许取值域，已忽略并保持最严",
                server, str(tool_name), value,
            )
            continue
        overrides[str(tool_name)] = contract
    return overrides


def apply_trust(
    server: str,
    tool: BaseTool,
    override: Mapping[str, EffectContract] | None = None,
) -> BaseTool:
    """按三层信任为一个外部工具签发契约；未命中任何一层即不签发。"""
    discard_declared_effect(tool)
    contract = shipped_contract(server, tool.name)
    if contract is None and override:
        contract = override.get(tool.name) or override.get("*")
    return declare_effect(tool, contract) if contract is not None else tool


def trust_tools(
    server: str,
    tools: Iterable[BaseTool],
    override: Mapping[str, EffectContract] | None = None,
) -> list[BaseTool]:
    """摄取边界：为一组外部工具按三层信任签发契约，返回同一顺序的工具列表。"""
    return [apply_trust(server, tool, override) for tool in tools]
