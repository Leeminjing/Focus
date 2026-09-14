"""本文件对外提供工具效果契约的类型、声明入口与结构化目标解析，是「这个工具的本地副作用能否被证明」的唯一归属地。

对外提供:
    ToolEffectKind — 四种互斥的效果分类
    ResolvedFsEffect — 一次调用被证明的全部受治理本地目标（读 / 写，0..N 个）
    EffectContract — 一条效果契约（分类 + 结构化目标解析器）
    OPAQUE_LOCAL_EFFECT / NO_LOCAL_EFFECT / DELEGATED_EXECUTION_EFFECT — 三个无数据的契约单例
    structured_fs(resolver) — 构造可结构化枚举的契约
    declare_effect(tool, contract) — 由 Focus 代码为某个工具签发契约
    declare_all_effects(tools, contract) — 为一组工具签发同一条契约
    effect_of(tool) — 读取某个工具已签发的契约；未签发一律按不透明
    has_declared_effect(tool) — 该工具是否已由 Focus 签发过契约（用于分类审计）
    resolve_fs_effect(contract, args, context) — 解析一次调用的受治理本地目标
    discard_declared_effect(tool) — 信任边界：清除工具自带的契约槽

输入:
    tool: BaseTool — 工具对象本身（契约挂在对象上，因此同名工具无法互相冒充）
    contract: EffectContract — 由 declare_effect 签发的契约
    args: Mapping — 该次调用的工具参数
    context: Mapping — 受治理的运行上下文（由装配层提供）

输出:
    effect_of → EffectContract；未签发时返回不透明契约，绝不返回放宽结论
    resolve_fs_effect → ResolvedFsEffect；非结构化契约恒返回空目标
    declare_effect / discard_declared_effect → 传入的工具对象（便于链式使用）

具体工作流:
    (1) 契约只由 Focus 代码签发并写入工具对象的 metadata 槽；读取侧未命中一律按不透明，
        因此「未声明即最严」是默认行为而非兜底分支
    (2) 外部执行工具（进程外第三方）自带的 metadata 不构成签发，摄取时由
        discard_declared_effect 清除；进程内工具与用户自装工具本身已在信任边界内，
        其声明等价于作者或用户的契约
    (3) 结构化契约的解析器只回答「这次调用会作用于哪些真实本地目标」，必须确定、幂等、
        无副作用；分类、路径规范化与准入判定都不在这里
    (4) 未完整枚举全部受治理目标的工具不得声明为结构化，只能按不透明处理

示例:
    declare_effect(read_file, structured_fs(_read_target_resolver))
    effect_of(some_unknown_tool).kind        # → ToolEffectKind.OPAQUE_LOCAL
    resolve_fs_effect(contract, {"path": "a.txt"}, {"workspace": "C:/ws"})
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

EFFECT_METADATA_KEY = "focus_effect_contract"


class ToolEffectKind(StrEnum):
    """工具效果分类：一次调用能否在执行前证明其受治理本地副作用。"""

    STRUCTURED_FS = "structured_fs"
    OPAQUE_LOCAL = "opaque_local"
    NO_LOCAL_EFFECT = "no_local_effect"
    DELEGATED_EXECUTION = "delegated_execution"


@dataclass(frozen=True)
class ResolvedFsEffect:
    """一次调用被证明的全部受治理本地目标，以及该次调用应当使用的参数。

    args 为 None 表示无需改写；非 None 时其中已把受治理目标替换为规范化真实路径，
    使「判定所依据的目标」与「工具体实际作用的目标」是同一个值。
    """

    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    args: Mapping[str, Any] | None = None


FsEffectResolver = Callable[[Mapping[str, Any], Mapping[str, Any]], ResolvedFsEffect]


@dataclass(frozen=True)
class EffectContract:
    """一条效果契约：分类，以及结构化分类下的目标解析器。"""

    kind: ToolEffectKind
    resolver: FsEffectResolver | None = None

    def __post_init__(self) -> None:
        structured = self.kind is ToolEffectKind.STRUCTURED_FS
        if structured and self.resolver is None:
            raise ValueError("可结构化枚举的契约必须提供目标解析器")
        if not structured and self.resolver is not None:
            raise ValueError("只有可结构化枚举的契约可以携带目标解析器")


OPAQUE_LOCAL_EFFECT = EffectContract(ToolEffectKind.OPAQUE_LOCAL)
NO_LOCAL_EFFECT = EffectContract(ToolEffectKind.NO_LOCAL_EFFECT)
DELEGATED_EXECUTION_EFFECT = EffectContract(ToolEffectKind.DELEGATED_EXECUTION)


def structured_fs(resolver: FsEffectResolver) -> EffectContract:
    """构造可结构化枚举的契约：解析器须确定、幂等、无副作用。"""
    return EffectContract(ToolEffectKind.STRUCTURED_FS, resolver)


def declare_effect(tool: BaseTool, contract: EffectContract) -> BaseTool:
    """由 Focus 代码为某个工具签发效果契约，返回该工具以便链式使用。"""
    metadata = dict(getattr(tool, "metadata", None) or {})
    metadata[EFFECT_METADATA_KEY] = contract
    tool.metadata = metadata
    return tool


def declare_all_effects(tools: Iterable[BaseTool], contract: EffectContract) -> list[BaseTool]:
    """为一组工具签发同一条契约，返回同一顺序的工具列表。"""
    return [declare_effect(tool, contract) for tool in tools]


def effect_of(tool: BaseTool | None) -> EffectContract:
    """读取某个工具已签发的效果契约；未签发一律按不透明处理。"""
    if tool is None:
        return OPAQUE_LOCAL_EFFECT
    contract = (getattr(tool, "metadata", None) or {}).get(EFFECT_METADATA_KEY)
    if isinstance(contract, EffectContract):
        return contract
    return OPAQUE_LOCAL_EFFECT


def has_declared_effect(tool: BaseTool | None) -> bool:
    """该工具是否已由 Focus 签发过契约；用于把「显式声明为不透明」与「未声明」区分开。"""
    if tool is None:
        return False
    return isinstance((getattr(tool, "metadata", None) or {}).get(EFFECT_METADATA_KEY), EffectContract)


def discard_declared_effect(tool: BaseTool) -> BaseTool:
    """清除工具自带的契约槽，用于进程外第三方工具的摄取边界。"""
    metadata = dict(getattr(tool, "metadata", None) or {})
    metadata.pop(EFFECT_METADATA_KEY, None)
    tool.metadata = metadata
    return tool


def resolve_fs_effect(
    contract: EffectContract,
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> ResolvedFsEffect:
    """解析一次调用的受治理本地目标；非结构化契约恒返回空目标。"""
    if contract.kind is not ToolEffectKind.STRUCTURED_FS or contract.resolver is None:
        return ResolvedFsEffect()
    resolved = contract.resolver(args, context)
    if not isinstance(resolved, ResolvedFsEffect):
        raise TypeError("可结构化枚举的解析器必须返回 ResolvedFsEffect")
    return resolved
