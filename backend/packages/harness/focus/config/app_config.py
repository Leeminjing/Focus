"""
本文件对外提供 get_app_config、reload_app_config 两个公开函数，以及 AppConfig 配置聚合类。

AppConfig: 声明式配置数据模型，聚合 models / stream_bridge / database / checkpointer / extensions 配置
get_app_config: 组合根入口，将 config.yaml 加载为全局单例 AppConfig 对象
reload_app_config: 强制刷新全局单例，修改 config.yaml 后立即生效

完整加载工作流：
_load_yaml 读取 YAML 文件 → resolve_env_vars 软解析 $ENV_VAR 环境变量引用（未命中的引用
原样保留，不阻塞加载，缺失校验推迟到使用期）
→ AppConfig.model_validate 由 dict 递归生成 AppConfig + 子 Pydantic 对象
→ 写入模块级 _app_config 单例缓存，后续 get_app_config 直接返回
"""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from focus.config.checkpointer_config import CheckpointerConfig
from focus.config.env import resolve_env_vars
from focus.config.commitment_config import CommitmentConfig
from focus.config.compression_config import CompressionConfig
from focus.config.database_config import DatabaseConfig
from focus.config.extensions_config import ExtensionsConfig
from focus.config.langgraph_store_config import LanggraphStoreConfig
from focus.config.model_config import ModelConfig
from focus.config.stream_bridge_config import StreamBridgeConfig


DEFAULT_MODEL_ENV_VAR = "FOCUS_MODEL"
"""用户覆盖默认模型的入口：环境变量优先于配置中的显式声明。"""


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    models: list[ModelConfig]
    stream_bridge: StreamBridgeConfig = StreamBridgeConfig()
    database: DatabaseConfig | None = None
    checkpointer: CheckpointerConfig = CheckpointerConfig()
    langgraph_store: LanggraphStoreConfig = LanggraphStoreConfig()
    extensions: ExtensionsConfig = ExtensionsConfig(mcp_servers={})
    commitment: CommitmentConfig = CommitmentConfig()
    compression: CompressionConfig = CompressionConfig()

    @model_validator(mode="after")
    def validate_curation_default(self) -> "AppConfig":
        defaults = [model.name for model in self.models if model.curation_default]
        if len(defaults) > 1:
            raise ValueError(f"只能配置一个策展默认模型: {', '.join(defaults)}")
        return self

    @model_validator(mode="after")
    def validate_default_model(self) -> "AppConfig":
        declared = [model.name for model in self.models if model.default]
        if len(declared) > 1:
            raise ValueError(f"只能声明一个默认模型: {', '.join(declared)}")
        return self

    def resolve_default_model_name(self) -> str:
        """解析默认模型名（默认模型的唯一真相来源）。

        优先级：环境变量 FOCUS_MODEL > 配置中显式声明 default: true 的条目。
        不再回退到列表位置，避免「调整条目顺序即改变系统行为」。
        """
        override = (os.environ.get(DEFAULT_MODEL_ENV_VAR) or "").strip()
        if override:
            if not any(model.name == override for model in self.models):
                raise ValueError(
                    f"{DEFAULT_MODEL_ENV_VAR} 指向不存在的模型条目: '{override}'"
                )
            return override

        declared = [model.name for model in self.models if model.default]
        if not declared:
            raise ValueError(
                "未声明默认模型: 请在 config.yaml 中为某个模型条目设置 default: true，"
                f"或设置 {DEFAULT_MODEL_ENV_VAR}"
            )
        return declared[0]

    def _normalize_name(self, name: str) -> str:
        return name.strip()

    @staticmethod
    def _get_model_path(use: str) -> tuple[str, str]:
        parts = use.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"无效的 use 格式: '{use}'，应为 'module:Class'")
        return parts[0], parts[1]

    def get_model(self, name: str) -> ModelConfig:
        normalized = self._normalize_name(name)
        for m in self.models:
            if m.name == normalized:
                return m
        raise KeyError(f"未找到模型配置: '{name}'")


_app_config: AppConfig | None = None


def _load_yaml(path: str) -> dict:
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_app_config(yaml_path: str) -> AppConfig:
    global _app_config
    if _app_config is None:
        _app_config = _load_layered_config(yaml_path)
    return _app_config


def reload_app_config(yaml_path: str) -> AppConfig:
    global _app_config
    _app_config = _load_layered_config(yaml_path)
    return _app_config


def _load_layered_config(yaml_path: str) -> AppConfig:
    """经两层聚合层读取配置：全局态 `~/.focus/config.yaml` 为默认，仓库态 <yaml_path> 优先。"""
    from focus.config.layered import load_layered_map

    raw = load_layered_map(Path(yaml_path).name, yaml_path)
    resolved = resolve_env_vars(raw)
    return AppConfig.model_validate(resolved)
