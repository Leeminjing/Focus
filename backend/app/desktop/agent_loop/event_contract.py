r"""本文件对外提供 CanonicalEventDraft、CanonicalEventEnvelope、EventVisibility 与 LiveEventAuthorizationPolicy。

输入为领域事件 identity、entity revision、关联 identity、可见性和 JSON payload；输出为版本化、可验证、可安全投递的事件信封。
具体工作流为 schema 接受符合命名规则的未来事件 kind，递归拒绝非 JSON/过深 payload，授权策略核对权限并移除秘密、
隐藏推理和未授权证据字段；连续事件流使用不泄露原事件身份的占位信封推进被隐藏 sequence。示例：
`policy.public_envelope(event, permissions)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REDACTED_KEYS = frozenset({
    "api_key",
    "authorization",
    "chain_of_thought",
    "cookie",
    "password",
    "private_reasoning",
    "raw_prompt",
    "reasoning_content",
    "secret",
    "token",
    "tool_arguments",
})


class _EventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventVisibility(_EventModel):
    audience: str = Field(default="loop_member", pattern=r"^(loop_member|operator)$")
    required_permissions: tuple[str, ...] = ()
    evidence_fields: tuple[str, ...] = ()


class CanonicalEventDraft(_EventModel):
    event_id: str | None = Field(default=None, min_length=1, max_length=64)
    schema_version: int = Field(default=1, ge=1)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", max_length=120)
    entity_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    entity_id: str = Field(min_length=1, max_length=120)
    entity_revision: int = Field(ge=1)
    correlation_id: str | None = Field(default=None, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)
    visibility: EventVisibility = Field(default_factory=EventVisibility)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    retained_until: datetime | None = None

    @field_validator("payload")
    @classmethod
    def validate_safe_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json(value, depth=0)
        return value


class CanonicalEventEnvelope(_EventModel):
    event_id: str
    loop_id: str
    sequence: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    kind: str
    entity_type: str
    entity_id: str
    entity_revision: int = Field(ge=1)
    correlation_id: str | None = None
    causation_id: str | None = None
    visibility: EventVisibility
    payload: dict[str, Any]
    idempotency_key: str
    occurred_at: datetime
    retained_until: datetime | None = None


class EventNotAuthorized(PermissionError):
    pass


class LiveEventAuthorizationPolicy:
    def public_envelope(
        self,
        envelope: CanonicalEventEnvelope,
        permissions: set[str] | frozenset[str],
    ) -> CanonicalEventEnvelope:
        try:
            return self.redact(envelope, permissions)
        except EventNotAuthorized:
            return envelope.model_copy(
                update={
                    "kind": "loop.event.redacted",
                    "entity_type": "redacted_event",
                    "entity_id": envelope.event_id,
                    "entity_revision": 1,
                    "correlation_id": None,
                    "causation_id": None,
                    "visibility": EventVisibility(),
                    "payload": {"status": "redacted"},
                    "idempotency_key": f"redacted:{envelope.event_id}",
                }
            )

    def redact(
        self,
        envelope: CanonicalEventEnvelope,
        permissions: set[str] | frozenset[str],
    ) -> CanonicalEventEnvelope:
        required = set(envelope.visibility.required_permissions)
        if not required.issubset(permissions):
            raise EventNotAuthorized("事件权限不足")
        payload = self._redact_value(envelope.payload)
        if "view_evidence" not in permissions:
            for field in envelope.visibility.evidence_fields:
                payload.pop(field, None)
        return envelope.model_copy(update={"payload": payload})

    @classmethod
    def _redact_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._redact_value(item) for key, item in value.items() if key.casefold() not in _REDACTED_KEYS}
        if isinstance(value, list):
            return [cls._redact_value(item) for item in value]
        return value


def _validate_json(value: Any, depth: int) -> None:
    if depth > 12:
        raise ValueError("event payload 嵌套过深")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("event payload key 必须是字符串")
            _validate_json(item, depth + 1)
        return
    raise ValueError("event payload 必须是 JSON 值")
