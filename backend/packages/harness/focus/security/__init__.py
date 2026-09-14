"""Focus 本地访问策略包入口，重导出路径解释与准入判定的公开 API。

对外提供:
    canonical_text / is_within / canonical_target — 真实宿主路径的规范化与归属判定
    AccessMode / AccessOperation / AccessDecision / AccessPolicy — 访问策略的类型
    decide_path_access / policy_from_context / restrictive_policy — 唯一准入判定与策略构造

输入:
    调用方从 focus.security 导入的公开类型与函数。

输出:
    路径解释结果、访问策略对象与判定结果。

具体工作流:
    (1) 路径解释只回答「这串值指向哪个真实路径」
    (2) 准入判定只回答「这个真实路径在当前策略下是放行还是交回人类」
    (3) 两件事分属两个模块，互不依赖对方的判断
    (4) 工具效果契约另有归属地（focus.security.effects）：它依赖工具框架，因此不在此重导出，
        使「纯策略逻辑」与「面向工具框架的契约」的边界在 import 上可见

示例:
    from focus.security import AccessOperation, decide_path_access, policy_from_context
    from focus.security.effects import NO_LOCAL_EFFECT, declare_effect
"""

from focus.security.paths import canonical_target, canonical_text, is_within
from focus.security.policy import (
    AccessDecision,
    AccessMode,
    AccessOperation,
    AccessPolicy,
    decide_path_access,
    policy_from_context,
    restrictive_policy,
)

__all__ = [
    "canonical_target",
    "canonical_text",
    "is_within",
    "AccessDecision",
    "AccessMode",
    "AccessOperation",
    "AccessPolicy",
    "decide_path_access",
    "policy_from_context",
    "restrictive_policy",
]
