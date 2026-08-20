"""dsh-eyes 插件入口:构建图片剥离 hook 与 view_image 工具声明。

对外提供:
    build_plugin(context) — 校验配置(模型/端点非空)后返回 PluginDeclaration

声明:
    tools: [view_image] — 经 PluginBridgeMiddleware 注入所有角色工具节点
    hooks: {"hook.before_model": [strip_images]} — 模型调用前剥离 image 块为文本引用

接入校验只检查配置字段非空(API key 在请求时才使用,缺失不影响接入);
视觉请求失败在工具内以错误文本返回,不中断主流程。
"""

from focus.plugins.schemas import PluginContext, PluginDeclaration

from plugins.dsh_eyes.config import endpoint_url, vision_model
from plugins.dsh_eyes.index import get_index
from plugins.dsh_eyes.strip import strip_images
from plugins.dsh_eyes.tool import view_image


class VisionService:
    """service.vision 接口实现:任何消费者可经 async describe(data_url) 获得视觉描述。"""

    name = "dashscope-qwen"

    async def describe(self, data_url: str) -> str:
        from plugins.dsh_eyes.config import load_config
        from plugins.dsh_eyes.eyes import describe_image

        return await describe_image(load_config(), data_url)


def build_plugin(context: PluginContext) -> PluginDeclaration:
    import logging

    from plugins.dsh_eyes.config import resolve_style

    logger = logging.getLogger(__name__)
    config = dict(context.config)
    # 对齐 dsh-eyes:模型/端点缺失仅告警不阻止加载(调用时失败);key 缺失同样仅请求时报
    try:
        model = vision_model(config)
    except RuntimeError:
        model = ""
        logger.warning("[dsh-eyes] no vision model configured; view_image will fail.")
    try:
        endpoint = endpoint_url(config)
    except RuntimeError:
        endpoint = ""
        logger.warning("[dsh-eyes] no vision endpoint configured; view_image will fail.")
    if endpoint:
        logger.info(
            "[dsh-eyes] vision endpoint: %s (api style: %s)", endpoint, resolve_style(config),
        )
    # 触发索引单例加载(损坏时重建)
    get_index()
    return PluginDeclaration(
        tools=[view_image],
        hooks={"hook.before_model": [strip_images]},
        services={"service.vision": VisionService()},
    )
