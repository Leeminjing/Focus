"""本文件对外提供模型能力查询，是「主模型能否接收图像输入」判定的唯一归属地。

对外提供:
    main_model_text_only — 判定当前主模型是否只能处理文本（无图像输入能力）

输入:
    app_config: AppConfig | None — 已解析的应用配置；为 None 时自行按 config.yaml 解析

输出:
    main_model_text_only → bool：True 表示纯文本模型（不具备图像输入能力）

具体工作流:
    (1) 取默认模型条目
    (2) 只依据该条目的 supports_image_input 显式声明判定，不从模型名推断
    (3) 声明缺失、模型条目缺失、配置不可解析一律按纯文本处理——保守语义保证
        图像内容不会被送进不接受图像的模型

示例:
    main_model_text_only(app_config)   # 条目未声明 supports_image_input → True
"""

from __future__ import annotations

from focus.config import AppConfig, get_app_config

DEFAULT_CONFIG_PATH = "config.yaml"


def main_model_text_only(app_config: AppConfig | None = None) -> bool:
    try:
        resolved = app_config or get_app_config(DEFAULT_CONFIG_PATH)
        model = resolved.get_model(resolved.resolve_default_model_name())
    except Exception:
        return True
    return not model.supports_image_input
