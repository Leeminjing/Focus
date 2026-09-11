"""本文件为 Python 测试提供跨文件复用的构造辅助，避免同一份配置模型在多处重复拼装。

对外提供:
    app_config_for — 构造只含一个模型条目的 AppConfig

输入:
    model_name: str — 模型条目名（同时作为 model 字段）
    supports_image_input: bool | None — 图像输入能力声明；None 表示不写该字段（测默认值路径）

输出:
    AppConfig — 可直接交给 resolve_default_model_name / get_model 使用

具体工作流:
    拼装最小可用模型条目（该条目声明为 default）→ 仅在显式传入时写入图像输入能力声明
    → 交给 AppConfig 校验并返回。

示例:
    config = app_config_for("deepseek-v4-flash", None)
"""

from focus.config.app_config import AppConfig


def app_config_for(model_name: str, supports_image_input: bool | None) -> AppConfig:
    entry: dict = {
        "name": model_name,
        "display_name": model_name,
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "model": model_name,
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com",
        "default": True,
    }
    if supports_image_input is not None:
        entry["supports_image_input"] = supports_image_input
    return AppConfig(models=[entry])
