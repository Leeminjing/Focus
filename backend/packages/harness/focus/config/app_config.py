"""
本文件对外提供 get_app_config、reload_app_config 两个公开函数，以及 AppConfig 配置聚合类。

AppConfig: 声明式配置数据模型，聚合 models / stream_bridge / database / checkpointer / extensions 配置
get_app_config: 组合根入口，将 config.yaml 加载为全局单例 AppConfig 对象
reload_app_config: 强制刷新全局单例，修改 config.yaml 后立即生效

完整加载工作流：
_load_yaml 读取 YAML 文件 → _resolve_env_vars 解析 $ENV_VAR 环境变量引用
→ AppConfig.model_validate 由 dict 递归生成 AppConfig + 子 Pydantic 对象
→ 写入模块级 _app_config 单例缓存，后续 get_app_config 直接返回
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from focus.config.checkpointer_config import CheckpointerConfig
from focus.config.env import resolve_env_var
from focus.config.commitment_config import CommitmentConfig
from focus.config.compression_config import CompressionConfig
from focus.config.database_config import DatabaseConfig
from focus.config.extensions_config import ExtensionsConfig
from focus.config.langgraph_store_config import LanggraphStoreConfig
from focus.config.model_config import ModelConfig
from focus.config.stream_bridge_config import StreamBridgeConfig


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


def _resolve_env_vars(data: dict) -> dict:
    resolved = {}
    for key, value in data.items():
        if isinstance(value, dict):
            resolved[key] = _resolve_env_vars(value)
        elif isinstance(value, list):
            resolved[key] = [
                _resolve_env_vars(item) if isinstance(item, dict) else _resolve_env_item(item)
                for item in value
            ]
        else:
            resolved[key] = _resolve_env_item(value)
    return resolved


def _resolve_env_item(value):
    resolved = resolve_env_var(value)
    if resolved is not None:
        return resolved
    if isinstance(value, str) and len(value) > 1 and value.startswith("$"):
        # 非标识符形式的 $ 前缀值仍按原语义硬失败（配置加载期契约）
        raise KeyError(f"环境变量未设置: {value[1:]}")
    return value


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
    resolved = _resolve_env_vars(raw)
    return AppConfig.model_validate(resolved)
