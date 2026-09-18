"""本文件对外提供受治理运行上下文键的声明、生产者声明与派生，是「哪些字段可以左右安全决策、
以及每个受治理字段由谁产生」的唯一归属地。

对外提供:
    declare_governed_keys(*keys) — 安全敏感消费者声明自己用于决策的运行上下文字段
    declare_governed_producer(key, producer) — 服务端生产者声明自己会产生某个受治理字段
    governed_keys() — 全部受治理键（消费者声明与生产者声明的并集）
    governed_producers() — 受治理键 → 生产者标识
    missing_producers() — 已受治理但无人生产的键（契约核对面）
    is_governed(key) — 单个键是否受治理
    strip_governed(data) — 从映射中剥除受治理键，只留附带载荷

输入:
    keys: str — 消费者读取用于决策的运行上下文字段名
    key: str — 受治理的运行上下文字段名
    producer: str — 该键的唯一服务端生产者标识（产生它的函数限定名）
    data: Mapping | None — 待处理的运行上下文

输出:
    governed_keys → frozenset[str]
    governed_producers → Mapping[str, str] — 键到生产者标识的只读视图
    missing_producers → frozenset[str] — 非空即"声明了却没人生产"，属于契约破坏
    strip_governed → dict — 不含任何受治理键的副本

具体工作流:
    (1) 消费者在各自模块导入期声明自己读取哪些字段做决策：安全上下文的扁平投影、准入判定、
        中间件、协作层、工具效果解析器与执行路由层
    (2) 受治理键集合是该声明的并集，不单独维护名单；新增消费者只需在自身模块声明，
        因此名单不会与实现漂移
    (3) 生产者同样在导入期声明"我会产生哪个受治理键"；同一键出现两个不同生产者即失败——
        受治理键只允许一个来源，第二个来源必须显式冲突而不是静默覆盖
    (4) 附带载荷并入运行上下文之前先经 strip_governed，使扁平键只可能来自安全上下文的机械投影
        或已声明的生产者
    (5) 声明与生产者必须一一对应：missing_producers() 非空表示存在"被声明却没有人为它负责"的键，
        这类键在运行时会静默缺失，因此由测试在实现期拦下

注意:
    集合随消费者与生产者模块的导入而完整——服务进程在启动时导入全部消费者与生产者；局部导入的
    进程只会看到已导入部分的并集，因此判据是「声明的并集」而不是「完整的固定名单」。

不列为受治理键的字段:
    `checkpoint_id` 是执行提示而非权柄：它只能在既定的执行命名空间内选择「从哪一版继续」，
    而命名空间本身受治理，因此无法借它跨越执行身份；它只在 resume 与派生的 fork 路径上由
    服务端提供，调用方无法到达该路径（网关要求运行上下文来自服务端登记的执行身份）。

示例:
    declare_governed_keys("must_view_materials")
    declare_governed_producer("must_view_materials", "focus.agents.must_view.project_material_context")
    payload = strip_governed(caller_supplied)
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

_GOVERNED: set[str] = set()
_PRODUCERS: dict[str, str] = {}


def declare_governed_keys(*keys: str) -> None:
    """登记一组用于安全决策的运行上下文字段。"""
    _GOVERNED.update(str(key) for key in keys if key)


def declare_governed_producer(key: str, producer: str) -> None:
    """登记某个受治理键的唯一服务端生产者；生产者声明同时隐含该键受治理。

    同一个键被两个不同生产者声明时立即失败：受治理键只允许一个来源，第二个来源必须显式冲突，
    而不是靠导入顺序决定谁赢。
    """
    if not key or not producer:
        return
    name = str(key)
    owner = str(producer)
    existing = _PRODUCERS.get(name)
    if existing is not None and existing != owner:
        raise RuntimeError(
            f"受治理键 {name} 出现第二个生产者: {existing} / {owner}"
        )
    _GOVERNED.add(name)
    _PRODUCERS[name] = owner


def governed_keys() -> frozenset[str]:
    """全部受治理键：消费者声明与生产者声明的并集。"""
    return frozenset(_GOVERNED)


def governed_producers() -> Mapping[str, str]:
    """受治理键到生产者标识的只读视图。"""
    return MappingProxyType(dict(_PRODUCERS))


def missing_producers() -> frozenset[str]:
    """已受治理但没有服务端生产者的键；非空即契约被破坏。"""
    return frozenset(_GOVERNED.difference(_PRODUCERS))


def is_governed(key: str) -> bool:
    """单个键是否受治理。"""
    return key in _GOVERNED


def strip_governed(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """剥除受治理键，只保留附带载荷。"""
    if not isinstance(data, Mapping):
        return {}
    return {key: value for key, value in data.items() if key not in _GOVERNED}
