"""本文件对外提供 redact_text 与 configured_secret_values 的敏感值边界。

输入为文本、实际敏感值或可选模型配置；输出为替换后的文本或仅供内部使用的秘密值集合。
具体工作流为收集已解析模型凭据及当前凭据环境变量，纯文本替换按长度递减执行；值不进入日志或持久读面。
示例：redact_text(title, configured_secret_values(config))；调用方自行决定是否发布被替换后的标题。
"""

import os
import re


def redact_text(text, secrets):
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def configured_secret_values(config=None):
    values = [getattr(model, "api_key", "") for model in getattr(config, "models", ())]
    values.extend(value for key, value in os.environ.items()
                  if re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", key, re.I))
    return tuple(v for v in set(values) if isinstance(v, str) and v and not v.startswith("$"))
