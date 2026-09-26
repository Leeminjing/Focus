r"""本文件对外提供 RoleBoundStructuredModel。

输入为 AppConfig、明确的 semantic derivation worker role、可选模型名、结构化 schema、authority prompt、冻结 payload 与可选结果验证器；
输出为经过有界生成/校验重试的 schema 结果、最近一次调用的 attempts 和累计 ModelUsage。具体工作流为每次 attempt 创建独立
StructuredWorkerModel，把 schema 或业务合同的稳定分类、unit identity 和违反规则反馈给下一请求，记录该次 usage 与结果，成功后
立即返回，耗尽重试后传播最后一个异常；本模块不读取 Context、修改状态或提交 Portfolio。示例：
`result = await model.invoke_validated(Schema, prompt, payload, validator)`。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import ValidationError

from focus.config.app_config import AppConfig
from focus.runtime.runs.usage import ModelUsage

from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel

DerivationWorkerRole = Literal[
    "semantic_index_projector",
    "work_spec_reconciler",
    "dossier_synthesizer",
    "claim_verifier",
    "context_quality_verifier",
]


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

    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        return await self._invoke(schema, system, payload, None)

    async def invoke_validated(
        self,
        schema,
        system: str,
        payload: dict[str, Any],
        validator: Callable[[Any], None],
    ):
        return await self._invoke(schema, system, payload, validator)

    async def _invoke(
        self,
        schema,
        system: str,
        payload: dict[str, Any],
        validator: Callable[[Any], None] | None,
    ):
        records: list[dict[str, Any]] = []
        invocation_usage = ModelUsage()
        last_error: Exception | None = None
        feedback: dict[str, str] | None = None
        for attempt in range(1, self._max_attempts + 1):
            worker = StructuredWorkerModel(self._app_config, self._model_name)
            try:
                result = await worker.invoke(
                    schema,
                    system,
                    self._attempt_payload(payload, feedback),
                )
                if validator is not None:
                    validator(result)
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
                    )
                )
                continue
            invocation_usage += worker.usage
            records.append(self._record(attempt, "success", worker.usage))
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
        feedback = {
            "category": category,
            "error_type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
        if isinstance(exc, StructuredResultValidationError):
            feedback.update(
                {
                    "unit_identity": exc.unit_identity,
                    "violated_rule": exc.violated_rule,
                }
            )
        return feedback
