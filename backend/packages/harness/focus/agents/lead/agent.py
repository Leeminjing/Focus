"""本文件对外提供 make_lead_agent 的统一角色执行图工厂。

输入为显式模型目录、稳定基础行为、实际工具与冻结 selected context、inbox／attempt 端口；输出为 CompiledStateGraph。
具体工作流为装配唯一 Provider 模型入口及安全最外层，依次处理耐久 inbox 和完整请求压缩预算，
历史重建后统一准备 WorldState、冻结 selected context、有效 replay 分支和 typed authority，
再由 create_agent 执行 sampling／工具交换。Scoped context 与运行状态进入 input，基础行为独立进入 instructions；
工具执行 ledger 保留不确定恢复边界；WorldState 与 typed bridge 在 sync checkpoint 提交，模型审计独立于任务语义。
helpers 只承担技能目录发现；Desktop ORM 不进入 Harness。attempt／inbox 端口由 Desktop 组合根注入。
示例：graph = await make_lead_agent(model_name="model", tools=tools, system_prompt=policy, frozen_contexts=contexts)。
"""

import json
from dataclasses import asdict
import logging
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from focus.agents.lead_agent_state import LeadAgentState
from focus.config import AppConfig, get_app_config
from focus.models import create_chat_model
from focus.plugins import get_plugin_registry
from focus.plugins.bridge import PluginBridgeMiddleware
from focus.security.middleware import AccessPolicyMiddleware
from focus.context.middleware import WorldStateMiddleware
from focus.context.scoped import FrozenContext
from focus.history.middleware import TypedHistoryMiddleware
from focus.runtime.tool_attempts import ToolExecutionMiddleware
from focus.tools import get_available_tools

logger = logging.getLogger(__name__)


def _load_enabled_skill_names() -> frozenset[str]:


    try:
        with open("extensions_config.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return frozenset()

    skills_cfg = raw.get("skills")
    if not isinstance(skills_cfg, dict):
        return frozenset()

    return frozenset(
        name for name, cfg in skills_cfg.items()
        if isinstance(cfg, dict) and cfg.get("enabled") is True
    )


def _discover_and_build_catalog(
    enabled_names: frozenset[str],
    host_base_path: str | None,
    user_id: str | None = None,
) -> "SkillCatalog":


    from focus.skills.catalog import SkillCatalog
    from focus.skills.parser import parse_skill_file
    from focus.skills.types import (
        SKILLS_PUBLIC_REAL_ROOT,
        SKILL_MD_FILE,
        SkillCategory,
        skills_custom_root,
    )

    base_path = Path(host_base_path) if host_base_path else Path(SKILLS_PUBLIC_REAL_ROOT)

    categories: list[tuple[Path, SkillCategory]] = [
        (base_path, SkillCategory.PUBLIC),
    ]

    if user_id is not None:
        custom_base = skills_custom_root(user_id)
        categories.append((custom_base, SkillCategory.CUSTOM))

    skills: list = []
    for cat_dir, category in categories:
        if not cat_dir.is_dir():
            continue
        for skill_file in cat_dir.rglob(SKILL_MD_FILE):
            skill = parse_skill_file(
                skill_file=skill_file,
                category=category,
                relative_path=skill_file.parent.relative_to(cat_dir),
            )
            if skill is None:
                continue

            skill.enabled = skill.name in enabled_names
            if skill.enabled:
                skills.append(skill)

    return SkillCatalog(skills)


async def make_lead_agent(
    model_name: str | None = None,
    agent_name: str | None = None,
    tool_groups: list[str] | None = None,
    user_id: str | None = None,
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    middlewares: list[AgentMiddleware] | None = None,
    additional_middlewares: list[AgentMiddleware] | None = None,
    app_config: AppConfig | None = None,
    middleware_skill_names: frozenset[str] | None = None,
    frozen_contexts: tuple[FrozenContext, ...] = (),
    world_skill_catalog: dict | None = None,
    inbox_middleware: AgentMiddleware | None = None,
    attempt_middleware: AgentMiddleware | None = None,
) -> CompiledStateGraph:

    model = create_chat_model(name=model_name, app_config=app_config)


    catalog = None


    if tools is None:
        from focus.tools.interfaces import ToolInfo

        pooled = await get_available_tools(tool_groups=tool_groups)
        tools = [t.tool() if isinstance(t, ToolInfo) else t for t in pooled]

        from focus.tools.builtins.describe_skill_tool import build_describe_skill_tool

        if catalog is None:
            enabled_names = _load_enabled_skill_names()
            catalog = _discover_and_build_catalog(enabled_names, host_base_path=None, user_id=user_id)
        describe_skill_tool = build_describe_skill_tool(catalog)
        tools = [describe_skill_tool] + tools


    if middlewares is None:
        from focus.agents.lead.middlewares import build_general_middlewares

        resolved_config = app_config or get_app_config("config.yaml")
        context7_tools_loader = None
        if resolved_config.commitment.enabled:
            from focus.mcp import get_context7_tools

            async def context7_tools_loader() -> list[BaseTool]:
                return await get_context7_tools(
                    resolved_config.commitment.context7_url
                )
        skill_names = middleware_skill_names
        if skill_names is None:
            skill_names = (
                frozenset(catalog.names) if catalog is not None else frozenset()
            )
        middlewares = build_general_middlewares(
            app_config=resolved_config,
            model=model,
            context7_tools_loader=context7_tools_loader,
            skill_names=skill_names,
        )
    if additional_middlewares:
        middlewares = [*(middlewares or []), *additional_middlewares]


    middlewares = [*(middlewares or []), PluginBridgeMiddleware(get_plugin_registry())]


    middlewares = [AccessPolicyMiddleware(), ToolExecutionMiddleware(), *middlewares]
    visible_tools = [*tools, *(tool for middleware in middlewares for tool in getattr(middleware, "tools", ()))]
    from focus.agents.compression.gate import CompressionGate
    from focus.models.response_projection import function_specs
    from focus.models.responses import FocusResponsesChatModel
    responses_model = model if isinstance(model, FocusResponsesChatModel) else None
    world_state = WorldStateMiddleware(
        visible_tools, system_prompt, middleware_skill_names or frozenset(),
        model_name or (app_config or get_app_config("config.yaml")).resolve_default_model_name(),
        responses_model.provider_contract.projection_version if responses_model else "focus-chat-bridge-v1",
        skill_catalog=world_skill_catalog,
        provider_contract=asdict(responses_model.provider_contract) if responses_model else None,
        frozen_contexts=frozen_contexts,
    )
    for configured in middlewares:
        if isinstance(configured, CompressionGate):
            configured.configure_request(system_prompt, function_specs(visible_tools), model=responses_model, world_state=world_state)
    middleware = [middlewares[0],
                  *([inbox_middleware] if inbox_middleware is not None else []), *middlewares[1:], world_state,
                  TypedHistoryMiddleware(), *([attempt_middleware] if attempt_middleware is not None else [])]


    return create_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=LeadAgentState,
        context_schema=dict,
    )
