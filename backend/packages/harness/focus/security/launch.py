"""本文件对外提供运行上下文的唯一组装入口 assemble_run_context，以及执行提示的生产者写回
project_dispatch_hints 与提示键常量 SWARM_DEPTH_CONTEXT_KEY。

输入为执行身份档案（ExecutionProfile）、附带载荷（调用方或启动点提供的随行数据）与执行提示
（唤醒链深度等描述本次执行如何被触发的参数）；输出为完整的运行上下文扁平字典——受治理键只来自
身份投影与已声明的服务端生产者，附带载荷先经 strip_governed 剥除，执行提示由本模块写回。

具体工作流为：
    (1) 由身份档案派生安全上下文并取它的扁平投影（唯一身份来源）
    (2) 并入剥除受治理键后的附带载荷（随行数据，永不构成受治理键的来源）
    (3) 写回执行提示生产者：唤醒链深度恒被写入（用户驱动的主 run 为 0），使读取点无需默认值、
        且每个启动点产出的上下文都包含全部受治理键

示例：
    profile = ExecutionProfile(authorization=..., routing=RoutingIdentity(..., task_id="t-1", ...))
    context = assemble_run_context(profile, {"skills": []}, dispatch_hints={"swarm_depth": 2})
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from focus.security.context import ExecutionProfile, derive_security_context
from focus.security.governed import declare_governed_producer, strip_governed

SWARM_DEPTH_CONTEXT_KEY = "swarm_depth"
"""唤醒链深度：0 表示用户驱动的执行，N 表示由消息驱动的第 N 层唤醒。"""

PRODUCER = "focus.security.launch.project_dispatch_hints"
"""本模块作为服务端生产者的稳定标识。"""

declare_governed_producer(SWARM_DEPTH_CONTEXT_KEY, PRODUCER)


def project_dispatch_hints(
    context: dict[str, Any], hints: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """把执行提示写回运行上下文，是这些键的唯一服务端生产者。

    输入:
        context: dict — 已含身份投影与附带载荷的运行上下文
        hints: Mapping | None — 执行提示；缺失的提示取该键的基准值（唤醒链深度取 0）

    输出:
        dict — 同一个 context（原地写回）
    """
    values = hints or {}
    raw = values.get(SWARM_DEPTH_CONTEXT_KEY, 0)
    context[SWARM_DEPTH_CONTEXT_KEY] = max(0, int(raw or 0))
    return context


def assemble_run_context(
    profile: ExecutionProfile,
    extras: Mapping[str, Any] | None = None,
    *,
    dispatch_hints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组装一次执行的运行上下文：身份投影 → 剥除后的附带载荷 → 执行提示写回。

    输入:
        profile: ExecutionProfile — 服务端拥有的执行身份档案
        extras: Mapping | None — 附带载荷（受治理键会被剥除）
        dispatch_hints: Mapping | None — 执行提示（唤醒链深度等）

    输出:
        dict — 完整运行上下文；受治理键只可能来自身份投影或已声明的生产者
    """
    projection = derive_security_context(profile).to_runtime_context()
    return project_dispatch_hints({**projection, **strip_governed(extras)}, dispatch_hints)
