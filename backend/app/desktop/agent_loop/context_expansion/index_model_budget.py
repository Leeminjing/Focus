r"""本文件对外提供 IndexModelBudget 与 BudgetedIndexModel。

输入为冻结资源策略、模型窗口和结构化 payload；输出为 attempt 准入、纯窗口预检或显式预算异常。
具体工作流为按完整请求 UTF-8 字节上界估计输入，预留输出／调用；短事务共享权威额度，结束时幂等结算真实用量。
resources 提供冻结策略；fits_request 只检查容量，admit 才为局部／综合／verifier attempts 预留额度。未知崩溃预留仍占额度。
BudgetedIndexModel.last_attempt_records 只包含本次 invocation：准入／配置拒绝返回空记录，已发送的失败或取消保留真实 attempts。
示例：model.fits_request(schema, prompt, multi_segment_payload)；多段原文超窗显式阻断，Tool 原子段不切开。
"""

import asyncio
import json

from backend.app.desktop.agent_loop.resource_limits import (
    exceeds_limit,
    remaining_capacity,
    request_limits,
)


class IndexBudgetExceeded(ValueError):
    pass


class IndexModelBudget:
    def __init__(self, resources, *, reservations=None) -> None:
        self._resources = resources
        self._calls = resources.global_model_calls_remaining
        self._input = resources.global_input_tokens_remaining
        self._output = resources.global_output_tokens_remaining
        self._lock = asyncio.Lock()
        self._reservations = reservations

    @property
    def has_reservations(self) -> bool:
        return self._reservations is not None

    @property
    def resources(self):
        return self._resources

    async def settle(self, usage) -> None:
        if self._reservations is not None:
            await self._reservations.settle(usage)

    @staticmethod
    def _estimated_input(schema, system, payload):
        request = json.dumps(
            {
                "system": system,
                "payload": payload,
                "schema": schema.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return len(request.encode("utf-8")) + 256

    def _request_limits(self, model):
        policy = self._resources.policy
        return request_limits(
            policy.max_request_input_tokens,
            policy.output_token_reserve,
            getattr(model, "context_window_tokens", None),
            int(getattr(model, "max_output_tokens", None) or policy.output_token_reserve or 8192),
        )

    def fits_request(self, model, schema, system, payload):
        limit, _ = self._request_limits(model)
        return not exceeds_limit(self._estimated_input(schema, system, payload), limit)

    async def admit(self, model, schema, system, payload) -> None:
        estimated = self._estimated_input(schema, system, payload)
        limit, output = self._request_limits(model)
        async with self._lock:
            if exceeds_limit(estimated, limit):
                raise IndexBudgetExceeded(
                    "semantic index provider_request_window exceeded; Tool Exchange remains indivisible"
                )
            if exceeds_limit(1, self._calls) or exceeds_limit(estimated, self._input) or exceeds_limit(output, self._output):
                raise IndexBudgetExceeded(
                    "semantic index authorized_model_budget exhausted"
                )
            if self._reservations is not None:
                await self._reservations.reserve(estimated, output)
            self._calls = remaining_capacity(self._calls, 1)
            self._input = remaining_capacity(self._input, estimated)
            self._output = remaining_capacity(self._output, output)


class BudgetedIndexModel:
    def __init__(self, model, budget: IndexModelBudget) -> None:
        self._model = model
        self._budget = budget
        self._identity = getattr(model, "cache_identity", None)
        self._guarded = hasattr(model, "bind_request_guard")
        self._last_attempt_records = ()
        if self._guarded:
            model.bind_request_guard(self._admit_checked)

    async def _admit_checked(self, model, schema, system, payload):
        self._check_identity()
        await self._budget.admit(model, schema, system, payload)

    def _check_identity(self):
        if getattr(self._model, "cache_identity", None) != self._identity:
            raise ValueError("semantic index model configuration 已 stale")

    @property
    def last_attempt_records(self):
        return self._last_attempt_records

    def fits_request(self, schema, system, payload):
        self._check_identity()
        return self._budget.fits_request(self._model, schema, system, payload)

    async def invoke(self, schema, system, payload):
        return await self._invoke(schema, system, payload)

    async def invoke_validated(self, schema, system, payload, validator):
        return await self._invoke(schema, system, payload, validator)

    async def _invoke(self, schema, system, payload, validator=None):
        self._last_attempt_records = ()
        self._check_identity()
        method = (
            getattr(self._model, "invoke_validated", None)
            if validator is not None
            else None
        )
        if not self._guarded:
            await self._budget.admit(self._model, schema, system, payload)
        try:
            if method is not None:
                return await method(schema, system, payload, validator)
            result = await self._model.invoke(schema, system, payload)
            if validator is not None:
                validator(result)
            return result
        finally:
            self._last_attempt_records = tuple(
                dict(attempt)
                for attempt in getattr(self._model, "last_attempt_records", ())
            )
