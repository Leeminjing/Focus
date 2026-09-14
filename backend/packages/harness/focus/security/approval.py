"""本文件对外提供越界批准请求的载荷与人工决定的解析，是「待决如何交回人类」的唯一归属地。

对外提供:
    APPROVAL_TYPE — 准入待决在中断载荷中的类型标记
    ApprovalRequest — 一次批准请求（工具、按操作类型分开的受治理真实目标、执行位置、发起角色、访问模式）
    ApprovalRequest.targets — 全部受治理目标的合并视图（去重保序）
    ApprovalRequest.payload() — 组装交给中断的中断载荷
    ApprovalRequest.from_payload(payload) — 由中断载荷还原请求；类型不符返回 None
    approval_granted(value) — 解析中断恢复值，判断是否为「仅允许这一次」

输入:
    payload: Mapping — 中断载荷
    value: Any — 中断恢复值，形如 {"decision": "approve" | "reject"}

输出:
    ApprovalRequest — 请求对象；from_payload → ApprovalRequest | None
    approval_granted → bool

具体工作流:
    (1) 请求只承载「人在决定前必须看到的东西」：请求了什么、落在哪个真实目标、在哪里执行、
        由谁发起、当前处于哪种访问模式
    (2) 受治理目标按操作类型分开承载（reads / writes）：同一个真实路径可能被读、被写或两者兼有，
        而「这个路径会不会被改写」正是人的判断依据，因此不合并成一张无标注的路径表
    (3) 载荷是纯数据，可被序列化进检查点并在重启后还原，因此恢复投影不需要额外结构
    (4) 决定只认 approve 与 reject 两种取值；无法识别一律按未批准处理（保守语义）

示例:
    request = ApprovalRequest(tool="bash", command="rm -rf x", cwd="C:/ws", access_mode="workspace")
    decision = interrupt(request.payload())
    if not approval_granted(decision):
        ...
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

APPROVAL_TYPE = "access_review"

_APPROVE = "approve"


def _texts(value: object) -> tuple[str, ...]:
    """把载荷里的字符串序列还原为元组；缺失或类型不符一律视为空。"""
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ApprovalRequest:
    """一次越界批准请求：人在决定前必须看到的全部信息。"""

    tool: str
    access_mode: str
    cwd: str
    agent_role: str | None = None
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    command: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def targets(self) -> tuple[str, ...]:
        """全部受治理目标的合并视图（去重保序），供只需要路径集合的调用方使用。"""
        return tuple(dict.fromkeys((*self.reads, *self.writes)))

    def payload(self) -> dict[str, Any]:
        """组装交给中断的载荷；字段缺失即缺失，不用占位值掩盖。"""
        payload: dict[str, Any] = {
            "type": APPROVAL_TYPE,
            "tool": self.tool,
            "access_mode": self.access_mode,
            "cwd": self.cwd,
            "reads": list(self.reads),
            "writes": list(self.writes),
        }
        if self.agent_role:
            payload["agent_role"] = self.agent_role
        if self.command:
            payload["command"] = self.command
        if self.extras:
            payload.update(self.extras)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "ApprovalRequest | None":
        """由中断载荷还原请求；不是准入待决时返回 None。"""
        if not isinstance(payload, Mapping) or payload.get("type") != APPROVAL_TYPE:
            return None
        return cls(
            tool=str(payload.get("tool") or ""),
            access_mode=str(payload.get("access_mode") or ""),
            cwd=str(payload.get("cwd") or ""),
            agent_role=str(payload["agent_role"]) if payload.get("agent_role") else None,
            reads=_texts(payload.get("reads")),
            writes=_texts(payload.get("writes")),
            command=str(payload["command"]) if payload.get("command") else None,
        )


def approval_granted(value: object) -> bool:
    """解析中断恢复值；只有明确的 approve 视为放行，其余一律未批准。"""
    if isinstance(value, Mapping):
        return str(value.get("decision") or "") == _APPROVE
    return False
