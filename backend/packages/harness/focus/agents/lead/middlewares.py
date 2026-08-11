"""
本文件对外提供 build_general_middlewares 共享中间件链组装函数。

对外提供:
    build_general_middlewares(app_config, model, context7_tools, skill_names) — 按 commitment.enabled
    条件装配 CommitmentMiddleware 的 AgentMiddleware 列表

输入:
    app_config: AppConfig | None — 组合根配置；None 时加载全局单例 config.yaml
    model: BaseChatModel | None — 当前 lead agent 已创建的模型
    context7_tools: list[BaseTool] | None — 异步工厂预先加载的隔离 Context7 工具
    skill_names: frozenset[str] — 当前任务可用技能名集合（触发剥离前导 skill token 用）

输出:
    list[AgentMiddleware] — 装配完成的中间件链

工作流:
    (1) 读取 app_config.commitment.enabled
    (2) 关闭时不实例化 CommitmentMiddleware，返回空链（与变更前一致）
    (3) 开启时要求当前模型和非空 Context7 工具，以 skill_names 构造 CommitmentMiddleware

示例:
    middlewares = build_general_middlewares(app_config, model, context7_tools, frozenset({"docx"}))
"""

import logging
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from focus.config import AppConfig, get_app_config

logger = logging.getLogger(__name__)


def build_general_middlewares(
    app_config: AppConfig | None = None,
    model: BaseChatModel | None = None,
    context7_tools: list[BaseTool] | None = None,
    skill_names: frozenset[str] = frozenset(),
) -> list[AgentMiddleware]:
    app_config = app_config or get_app_config("config.yaml")
    if not app_config.commitment.enabled:
        return []
    if model is None:
        raise RuntimeError("CommitmentMiddleware 已启用，但未提供当前 lead agent 模型")
    if not context7_tools:
        raise RuntimeError("CommitmentMiddleware 已启用，但 Context7 工具不可用")
    from focus.agents.commitment import CommitmentMiddleware

    middleware = CommitmentMiddleware(
        model=model,
        context7_tools=context7_tools,
        skill_names=frozenset(skill_names),
    )
    logger.info("承诺层已装配（commitment.enabled=true），skill_names=%d 个", len(skill_names))
    return [middleware]
