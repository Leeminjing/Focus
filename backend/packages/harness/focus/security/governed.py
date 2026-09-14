"""本文件对外提供受治理运行上下文键的声明与派生，是「哪些字段可以左右安全决策」的唯一归属地。

对外提供:
    declare_governed_keys(*keys) — 安全敏感消费者声明自己用于决策的运行上下文字段
    governed_keys() — 全部受治理键（消费者声明的并集）
    is_governed(key) — 单个键是否受治理
    strip_governed(data) — 从映射中剥除受治理键，只留附带载荷

输入:
    keys: str — 消费者读取用于决策的运行上下文字段名
    data: Mapping | None — 待处理的运行上下文

输出:
    governed_keys → frozenset[str]
    strip_governed → dict — 不含任何受治理键的副本

具体工作流:
    (1) 消费者在各自模块导入期声明自己读取哪些字段做决策：安全上下文的扁平投影、准入判定、
        中间件、协作层、工具效果解析器与执行路由层
    (2) 受治理键集合是该声明的并集，不单独维护名单；新增消费者只需在自身模块声明，
        因此名单不会与实现漂移
    (3) 附带载荷并入运行上下文之前先经 strip_governed，使扁平键只可能来自安全上下文的机械投影
    (4) 调用方提供的受治理字段一律不参与决策；本模块只负责界定与剥除，判定由调用点决定

注意:
    集合随消费者模块的导入而完整——服务进程在启动时导入全部消费者；局部导入的进程只会
    看到已导入部分的并集，因此判据是「声明的并集」而不是「完整的固定名单」。

不列为受治理键的字段:
    `checkpoint_id` 是执行提示而非权柄：它只能在既定的执行命名空间内选择「从哪一版继续」，
    而命名空间本身受治理，因此无法借它跨越执行身份；它只在 resume 与派生的 fork 路径上由
    服务端提供，调用方无法到达该路径（网关要求运行上下文来自服务端登记的执行身份）。

示例:
    declare_governed_keys("must_view_materials")
    payload = strip_governed(caller_supplied)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_GOVERNED: set[str] = set()


def declare_governed_keys(*keys: str) -> None:
    """登记一组用于安全决策的运行上下文字段。"""
    _GOVERNED.update(str(key) for key in keys if key)


def governed_keys() -> frozenset[str]:
    """全部受治理键：消费者声明的并集。"""
    return frozenset(_GOVERNED)


def is_governed(key: str) -> bool:
    """单个键是否受治理。"""
    return key in _GOVERNED


def strip_governed(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """剥除受治理键，只保留附带载荷。"""
    if not isinstance(data, Mapping):
        return {}
    return {key: value for key, value in data.items() if key not in _GOVERNED}
