"""spatial-patrol 插件入口:构建空间观察工具声明。

对外提供:
    build_plugin(context) — 视觉能力校验通过后返回 PluginDeclaration(tools=观察工具)

视觉能力判定:主模型为 text-only(如 DeepSeek V4)且无 vision 条目且无视觉插件
(service.vision 提供者)时抛 RuntimeError,loader 将插件标记为 Unavailable
并提醒「纯文本模型且无视觉插件」;其余系统不受影响。
"""

from focus.plugins.schemas import PluginContext, PluginDeclaration

from plugins.spatial_patrol import spatial


def build_plugin(context: PluginContext) -> PluginDeclaration:
    """初始化观察服务(含视觉能力判定)。

    观察工具不在此全局声明:插件 tool 接口会注入所有角色,而空间上下文只存在于
    空间小兵 run —— 观察工具仅由空间小兵 agent_factory 装配(routes._launch_spatial_run)。
    """
    spatial.init_service(context.config, context.registry)
    return PluginDeclaration()
