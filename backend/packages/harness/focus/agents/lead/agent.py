"""
本文件对外提供 make_lead_agent 异步工厂函数，作为 lead_agent 装配的唯一对外入口。

对外提供:
    make_lead_agent — 装配并返回可执行的 CompiledStateGraph

输入:
    make_lead_agent:
        model_name: str | None — 目标模型名，None 时取 config.yaml 中 models[0] 作为默认
        agent_name: str | None — system prompt 中的 agent 名称，None 时使用默认值 "focus"
        tool_groups: list[str] | None — 需要加载的工具分组名列表，None 表示加载全部
        user_id: str | None — 用户标识，用于定位 per-user custom skills 路径，None 时跳过 custom
        tools: list[BaseTool] | None — 自定义工具集，非 None 时跳过全局工具汇集与 describe_skill_tool
        system_prompt: str | None — 自定义系统提示词，非 None 时跳过技能扫描与模板生成
        middlewares: list[AgentMiddleware] | None — 自定义中间件链，非 None 时覆盖默认构建
        additional_middlewares: list[AgentMiddleware] | None — 追加到默认或自定义链末尾的中间件

输出:
    CompiledStateGraph — langchain.agents.create_agent() 产出的可执行 agent graph

具体工作流:
    (1) 调用 create_chat_model(name=model_name) 获取 BaseChatModel 实例
    (2) 若 system_prompt 为 None:
        (2a) 加载 skills（public/custom 扫描 + enabled 过滤 → SkillCatalog）
        (2b) apply_prompt_template(agent_name, skill_names, container_base_path) 生成 system_prompt
    (3) 若 tools 为 None: await get_available_tools(tool_groups=tool_groups) 汇集全局工具
        + build_describe_skill_tool(catalog) 创建 skill 查询工具
    (4) 若 middlewares 为 None: 使用空中间件链（沙箱/上传中间件已随网页端与沙箱移除）
    (4.5) 最终链最末端追加 PluginBridgeMiddleware（插件工具并入工具节点、插件 hook
        稳定顺序分发；无插件时为空操作）
    (5) 调用 langchain.agents.create_agent(model, tools, middleware, system_prompt, state_schema=LeadAgentState)
    (6) 返回 CompiledStateGraph

示例:
    graph = await make_lead_agent()
    graph = await make_lead_agent(model_name="deepseek-v4-flash", agent_name="DeepSeek")
    graph = await make_lead_agent(tool_groups=["file:read", "bash"])
    graph = await make_lead_agent(user_id="uuid-xxx")
    graph = await make_lead_agent(tools=my_tools, system_prompt=my_prompt, middlewares=[])
"""

import json
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
from focus.tools import get_available_tools

logger = logging.getLogger(__name__)


def _load_enabled_skill_names() -> frozenset[str]:
    """从 extensions_config.json 读取已启用的 skill 名称集合。

    输入: 无

    输出:
        frozenset[str] — enabled=true 的 skill 名称集合

    工作流:
        (1) 读取 extensions_config.json
        (2) 提取 skills 段
        (3) 过滤 enabled=true → 返回名称 frozenset
        (4) 文件不存在或 skills 段为空 → 返回空 frozenset
    """
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
    """扫描宿主机文件系统发现 SKILL.md，解析并构建 SkillCatalog。

    输入:
        enabled_names: frozenset[str] — extensions_config.json 中 enabled=true 的 skill 名称
        host_base_path: str | None — skills 根目录的宿主机路径，None 时从 SKILLS_PUBLIC_REAL_ROOT 取默认值
        user_id: str | None — 用户标识，用于定位 per-user custom skills 路径，None 时跳过 custom

    输出:
        SkillCatalog — 包含所有已启用 skill 的不可变索引

    工作流:
        (1) public: 遍历 SKILLS_PUBLIC_REAL_ROOT 下的 SKILL.md
        (2) custom: 若 user_id 非 None，遍历 SKILLS_CUSTOM_REAL_ROOT.format(user_id=user_id) 下的 SKILL.md
        (3) 逐个 parse_skill_file() 解析
        (4) 根据 enabled_names 过滤 enabled=true
        (5) 构建 SkillCatalog 并返回
    """
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
            # 设置 enabled 状态
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
) -> CompiledStateGraph:
    # (1) 创建模型
    model = create_chat_model(name=model_name, app_config=app_config)

    # (2) system prompt：桌面路径已注入（prompt_with_skills 等），此处不再生成默认模板
    catalog = None

    # (3) 汇集工具：未注入时走全局工具池 + describe_skill_tool
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

    # (4) middleware：未注入时经共享 builder 按 commitment.enabled 装配承诺层；
    #     Context7 只提供延迟加载函数，网络连接由承诺阶段首次实际使用时建立。
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
    # 插件桥接：位于最终链最末端（所有系统中间件之后），插件工具经 middleware tools
    # 属性并入工具节点、插件 hook 按注册表稳定顺序分发（全部角色统一生效）
    middlewares = [*(middlewares or []), PluginBridgeMiddleware(get_plugin_registry())]
    middleware = middlewares if middlewares is not None else []

    # (5) create_agent
    return create_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=LeadAgentState,
    )
