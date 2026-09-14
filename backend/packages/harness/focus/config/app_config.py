"""
本文件对外提供 get_app_config、reload_app_config、build_app_config、apply_app_config 四个公开函数，
以及 AppConfig 配置聚合类。

AppConfig: 声明式配置数据模型，聚合 models / stream_bridge / database / checkpointer / extensions 配置
get_app_config: 组合根入口，将 config.yaml 加载为全局单例 AppConfig 对象
reload_app_config: 强制替换全局单例（仅用于测试与显式重载；运行期生效走 apply_app_config）
build_app_config: 由两层原始 map 构造一个已校验的 AppConfig（不触碰全局单例）
apply_app_config: 把已校验对象就地写入既有对象，保持对象身份不变

完整加载工作流：
load_layered_maps 读取 文件层 + 用户偏好层 两份原始 map → merge_model_layers 施加模型目录专项合并
（按条目名覆盖、删除名单、单例标记归属）→ resolve_env_vars 软解析 $ENV_VAR 环境变量引用
（未命中的引用原样保留，不阻塞加载，缺失校验推迟到使用期）
→ AppConfig.model_validate 由 dict 递归生成 AppConfig + 子 Pydantic 对象
→ 写入模块级 _app_config 单例缓存，后续 get_app_config 直接返回

运行期生效的约束（Pydantic v2 语义）：
模型默认可变，但赋值与 model_copy(update=...) 都不触发校验。因此生效路径 MUST 先用
build_app_config 构造一个校验通过的对象，再用 apply_app_config 就地写入既有对象；
MUST NOT 依赖 reload_app_config——它替换模块全局，会让已经按引用持有旧对象的调用方
（桌面服务、巡检服务、策展引擎）继续读到旧配置。
"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

_MODEL_ENTRY_HINT = (
    "修改入口：桌面设置的「模型」区块，或配置文件（~/.focus/config.yaml 优先于当前目录的 config.yaml）"
)


def available_model_names(config: "AppConfig") -> str:
    """列出当前生效目录的条目名，供失败信息自解释（配置层与桌面服务共用同一实现）。"""
    return ", ".join(model.name for model in config.models) or "（当前没有任何条目）"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    models: list[ModelConfig]
    removed_models: list[str] = Field(default_factory=list)
    """用户偏好层的显式删除名单：这些条目名在合并后已被剔除，界面据此显示「已删除」。"""
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
        不再回退到列表位置，避免「调整条目顺序即改变系统行为」；失败信息列出全部可用条目，
        使拼写错误可以就地自救。
        """
        override = (os.environ.get(DEFAULT_MODEL_ENV_VAR) or "").strip()
        if override:
            if not any(model.name == override for model in self.models):
                raise ValueError(
                    f"{DEFAULT_MODEL_ENV_VAR} 指向不存在的模型条目: '{override}'；"
                    f"可用条目: {available_model_names(self)}。{_MODEL_ENTRY_HINT}"
                )
            return override

        declared = [model.name for model in self.models if model.default]
        if not declared:
            raise ValueError(
                "未声明默认模型: 请为某个模型条目设置 default: true，"
                f"或设置 {DEFAULT_MODEL_ENV_VAR}；可用条目: {available_model_names(self)}。"
                f"{_MODEL_ENTRY_HINT}"
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
        raise KeyError(
            f"未找到模型配置: '{name}'；可用条目: {available_model_names(self)}。{_MODEL_ENTRY_HINT}"
        )


_app_config: AppConfig | None = None


def build_app_config(file_map: dict, preference_map: dict) -> AppConfig:
    """由 文件层 + 用户偏好层 两份原始 map 构造一个已校验的 AppConfig（不写入全局单例）。

    环境变量引用在此软解析；未命中的引用原样保留，缺失校验推迟到使用期。
    """
    from focus.config.model_layers import merge_model_layers

    merged = merge_model_layers(file_map, preference_map)
    return AppConfig.model_validate(resolve_env_vars(merged))


def apply_app_config(target: AppConfig, fresh: AppConfig) -> AppConfig:
    """把已校验的 fresh 就地写入 target，保持 target 的对象身份不变，返回 target。

    就地写入是刻意的：桌面服务、巡检服务与策展引擎都按引用持有同一个配置对象，
    保持身份即可让它们无需任何刷新调用就读到新值。校验由 fresh 承担（见模块文档字符串）。
    """
    for name in type(fresh).model_fields:
        setattr(target, name, getattr(fresh, name))
    for name, value in (fresh.__pydantic_extra__ or {}).items():
        setattr(target, name, value)
    return target


def get_app_config(yaml_path: str) -> AppConfig:
    global _app_config
    if _app_config is None:
        _app_config = _load_layered_config(yaml_path)
    return _app_config


def reload_app_config(yaml_path: str) -> AppConfig:
    """强制替换全局单例。仅供测试与显式重载使用；运行期生效请用 apply_app_config。"""
    global _app_config
    _app_config = _load_layered_config(yaml_path)
    return _app_config


def _load_layered_config(yaml_path: str) -> AppConfig:
    """经两层聚合层读取配置：文件层 <yaml_path> 为默认，用户偏好层 `~/.focus/config.yaml` 优先。"""
    from focus.config.layered import load_layered_maps

    file_map, preference_map = load_layered_maps(Path(yaml_path).name, yaml_path)
    return build_app_config(file_map, preference_map)
