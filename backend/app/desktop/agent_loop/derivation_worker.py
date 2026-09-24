r"""本文件对外提供 RoleBoundStructuredModel。

输入为 AppConfig、明确的 semantic derivation worker role、可选模型名、结构化 schema、authority prompt 与冻结 payload；输出为经过
有界重试的 schema 结果、最近一次调用的 attempts 和累计 ModelUsage。具体工作流为每次 attempt 创建独立 StructuredWorkerModel，
记录该次 usage 与成功或错误类型，成功后立即返回，耗尽重试后传播最后一个异常；本模块不读取 Context、修改状态或提交 Portfolio。
示例：`model = RoleBoundStructuredModel(config, "dossier_synthesizer"); result = await model.invoke(Schema, prompt, payload)`。
"""

from __future__ import annotations

from typing import Any, Literal

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
        records: list[dict[str, Any]] = []
        invocation_usage = ModelUsage()
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            worker = StructuredWorkerModel(self._app_config, self._model_name)
            try:
                result = await worker.invoke(schema, system, payload)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                invocation_usage += worker.usage
                records.append(self._record(attempt, "error", worker.usage, type(exc).__name__))
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
    ) -> dict[str, Any]:
        return {
            "role": self.role,
            "attempt": attempt,
            "outcome": outcome,
            "model_calls": usage.model_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "error_type": error_type,
        }
