r"""本文件对外提供 RoleBoundStructuredModel、结构化结果验证异常及 safe_validation_message。

输入为 AppConfig、无权派生 role、schema、冻结 payload、结果 validator 及可选逐 attempt request guard；输出为结构化结果和真实 attempts／usage。
具体工作流为每次 attempt 新建独立 StructuredWorkerModel，先准入，再有界调用／验证；同步校验直接执行，异步返回确实等待；失败、取消和预算中止保存实际用量。
cache_identity 只哈希非凭据模型配置；bind_request_guard 为索引共享预算提供逐次检查，不读 Context 或提交 Portfolio。safe_validation_message 排除 Pydantic 的原候选输入，供反馈和持久失败共用。
示例：model.bind_request_guard(budget.admit); result = await model.invoke_validated(Schema, prompt, payload, validator)。
"""

from __future__ import annotations

import asyncio
import json
from inspect import isawaitable
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, Literal

from focus.config.app_config import AppConfig
from focus.runtime.runs.usage import ModelUsage
from pydantic import ValidationError

from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel

DerivationWorkerRole = Literal[
    "semantic_index_projector",
    "work_spec_reconciler",
    "dossier_synthesizer",
    "claim_verifier",
    "context_quality_verifier",
]


def safe_validation_message(exc):
    if isinstance(exc, ValidationError):
        return json.dumps(exc.errors(include_input=False, include_context=False, include_url=False), ensure_ascii=False)
    return str(exc)


class StructuredResultValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        unit_identity: str,
        violated_rule: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.unit_identity = unit_identity
        self.violated_rule = violated_rule


class RoleBoundStructuredModel:
    def __init__(
        self,
        app_config: AppConfig,
        role: DerivationWorkerRole,
        model_name: str | None = None,
        *,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("derivation worker max_attempts 必须为正数")
        self.role = role
        self._app_config = app_config
        self._model_name = model_name
        self._max_attempts = max_attempts
        self.last_attempt_records: tuple[dict[str, Any], ...] = ()
        self.last_usage = ModelUsage()
        self.usage = ModelUsage()
        self._request_guard = None
        self._usage_receipts = ContextVar("owned_model_usage", default=None)

    def bind_usage_receipts(self, receipts) -> None:
        self._usage_receipts.set(receipts)

    @property
    def usage_managed(self) -> bool:
        return self._usage_receipts.get() is not None

    @property
    def cache_identity(self) -> str:
        config = self._app_config.get_model(
            self._model_name or self._app_config.resolve_default_model_name()
        )
        payload = {
            key: value
            for key, value in config.model_dump(mode="json").items()
            if key not in {"api_key", "display_name", "default", "curation_default"}
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def bind_request_guard(self, guard) -> None:
        self._request_guard = guard

    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        return await self._invoke(schema, system, payload, None)

    async def invoke_validated(
        self,
        schema,
        system: str,
        payload: dict[str, Any],
        validator: Callable[[Any], None | Awaitable[None]],
    ):
        return await self._invoke(schema, system, payload, validator)

    async def _invoke(
        self,
        schema,
        system: str,
        payload: dict[str, Any],
        validator: Callable[[Any], None | Awaitable[None]] | None,
    ):
        records: list[dict[str, Any]] = []
        invocation_usage = ModelUsage()
        last_error: Exception | None = None
        feedback: dict[str, str] | None = None
        for attempt in range(1, self._max_attempts + 1):
            worker = StructuredWorkerModel(self._app_config, self._model_name)
            if self._usage_receipts.get() is not None:
                worker.bind_usage_receipts(self._usage_receipts.get())
            if self._request_guard is not None:
                try:
                    await self._request_guard(
                        worker, schema, system, self._attempt_payload(payload, feedback)
                    )
                except BaseException:
                    self._finish(invocation_usage, records)
                    raise
            try:
                result = await worker.invoke(
                    schema,
                    system,
                    self._attempt_payload(payload, feedback),
                )
                if validator is not None:
                    validation = validator(result)
                    if isawaitable(validation):
                        await validation
            except asyncio.CancelledError:
                invocation_usage += worker.usage
                records.append(
                    self._record(
                        attempt,
                        "cancelled",
                        worker.usage,
                        model_metadata=getattr(worker, "last_model_metadata", {}),
                    )
                )
                self._finish(invocation_usage, records)
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                invocation_usage += worker.usage
                category = self._failure_category(exc)
                feedback = self._failure_feedback(exc, category)
                records.append(
                    self._record(
                        attempt,
                        "error",
                        worker.usage,
                        type(exc).__name__,
                        category,
                        feedback,
                        model_metadata=getattr(worker, "last_model_metadata", {}),
                    )
                )
                continue
            invocation_usage += worker.usage
            records.append(
                self._record(
                    attempt,
                    "success",
                    worker.usage,
                    model_metadata=getattr(worker, "last_model_metadata", {}),
                )
            )
            self._finish(invocation_usage, records)
            return result
        self._finish(invocation_usage, records)
        if last_error is None:
            raise RuntimeError("derivation worker 未执行")
        raise last_error

    def _finish(self, usage: ModelUsage, records: list[dict[str, Any]]) -> None:
        self.last_usage = usage
        self.usage += usage
        self.last_attempt_records = tuple(records)

    def _record(
        self,
        attempt: int,
        outcome: str,
        usage: ModelUsage,
        error_type: str | None = None,
        failure_category: str | None = None,
        validation_feedback: dict[str, str] | None = None,
        model_metadata: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "role": self.role,
            "attempt": attempt,
            "outcome": outcome,
            "model_calls": usage.model_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "error_type": error_type,
            "failure_category": failure_category,
            "validation_feedback": validation_feedback,
            "model_metadata": model_metadata or {},
            "usage_reported": bool((model_metadata or {}).get("usage_reported")),
            "usage_managed": self.usage_managed,
        }

    @staticmethod
    def _attempt_payload(
        payload: dict[str, Any],
        feedback: dict[str, str] | None,
    ) -> dict[str, Any]:
        if feedback is None:
            return payload
        return {
            **payload,
            "previous_attempt_failure": feedback,
            "retry_instruction": "Correct the previous failure and return a fresh response matching the requested schema.",
        }

    @staticmethod
    def _failure_category(exc: Exception) -> str:
        if isinstance(exc, StructuredResultValidationError):
            return exc.code
        if isinstance(exc, (ValidationError, json.JSONDecodeError)):
            return "model_schema_error"
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return "infrastructure_error"
        if isinstance(exc, ValueError):
            return "model_schema_error"
        return "model_worker_error"

    @staticmethod
    def _failure_feedback(exc: Exception, category: str) -> dict[str, str]:
        message = safe_validation_message(exc)
        feedback = {
            "category": category,
            "error_type": type(exc).__name__,
            "message": message[:1000],
        }
        if isinstance(exc, StructuredResultValidationError):
            feedback.update(
                {
                    "unit_identity": exc.unit_identity,
                    "violated_rule": exc.violated_rule,
                }
            )
        return feedback
