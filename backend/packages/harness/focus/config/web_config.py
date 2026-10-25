"""本文件对外提供 WebToolsConfig 与 get_deepseek_api_key，管理联网工具凭据。

输入为 config.yaml 的 web_tools.api_key，可为字面密钥或 $VAR / ${VAR} 引用；
输出为实际调用使用的密钥，与模型目录及 Run 的模型选择无关。
具体工作流为 AppConfig 分层加载与软解析，再在工具使用期读取当前配置并校验引用；
缺少凭据时抛 ValueError，由工具边界转为 ToolException，不阻塞配置加载。
启动期由既有 load_global_dotenv 加载 ~/.focus/.env；进程环境变量优先。
示例：web_tools: {api_key: $DEEPSEEK_API_KEY}；也可显式引用已有的 $OPENAI_API_KEY。
"""

from focus.config.env import require_env_var
from pydantic import BaseModel, Field


class WebToolsConfig(BaseModel):
    api_key: str = Field(default="$DEEPSEEK_API_KEY", repr=False)


def get_deepseek_api_key() -> str:
    """使用期消费当前应用的联网配置；不根据 Agent 所选模型推断密钥。"""
    from focus.config.app_config import get_app_config

    configured = get_app_config("config.yaml").web_tools.api_key
    key = require_env_var(configured, context="DeepSeek 联网工具 web_tools.api_key").strip()
    if not key:
        raise ValueError("web_tools.api_key 不能为空，请在 config.yaml 中配置密钥或环境变量引用")
    return key
