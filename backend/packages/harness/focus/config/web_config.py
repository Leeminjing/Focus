"""联网工具的独立凭据入口，与模型目录及 Run 的模型选择无关。

启动期由既有 load_global_dotenv 加载 ~/.focus/.env；进程环境变量优先。
使用期才解析 DEEPSEEK_API_KEY，未配置时抛 ValueError，由工具边界转为 ToolException。
示例：key = get_deepseek_api_key()；不回退到 OPENAI_API_KEY。
"""

from focus.config.env import require_env_var


def get_deepseek_api_key() -> str:
    """使用期读取并校验独立的 DeepSeek 联网凭据，不在模块加载时要求配置。"""
    key = require_env_var("$DEEPSEEK_API_KEY", context="DeepSeek 联网工具").strip()
    if not key:
        raise ValueError("DEEPSEEK_API_KEY 不能为空（DeepSeek 联网工具需要它）")
    return key
