r"""本文件对外提供 IndexModelBudget 与 BudgetedIndexModel。

输入为冻结资源策略、模型单请求窗口和结构化 payload；输出为逐次 attempt 的准入或显式预算异常。
工作流为按完整请求的 UTF-8 字节上界估计输入，预留输出／调用次数；真实构建通过短事务共享权威额度，结束时幂等结算实际用量。
无持久化端口的纯适配器保留单构建锁；崩溃未结算预留仍占额度，不能由超时或重试自动释放。
示例：model = BudgetedIndexModel(port, budget)；零预算不会发送请求，Tool 原子段不切开。
"""

import asyncio
import json


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

    async def settle(self, usage) -> None:
        if self._reservations is not None:
            await self._reservations.settle(usage)

    async def admit(self, model, schema, system, payload) -> None:
        request = json.dumps(
            {
                "system": system,
                "payload": payload,
                "schema": schema.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        estimated = len(request.encode("utf-8")) + 256
        policy = self._resources.policy
        output = max(
            policy.output_token_reserve,
            int(getattr(model, "max_output_tokens", policy.output_token_reserve)),
        )
        window = getattr(model, "context_window_tokens", None)
        limit = policy.max_request_input_tokens
        if window is not None:
            limit = min(limit, window - output)
        async with self._lock:
            if estimated > limit:
                raise IndexBudgetExceeded(
                    "semantic index provider_request_window exceeded; Tool Exchange remains indivisible"
                )
            if self._calls < 1 or self._input < estimated or self._output < output:
                raise IndexBudgetExceeded(
                    "semantic index authorized_model_budget exhausted"
                )
            if self._reservations is not None:
                await self._reservations.reserve(estimated, output)
            self._calls -= 1
            self._input -= estimated
            self._output -= output


class BudgetedIndexModel:
    def __init__(self, model, budget: IndexModelBudget) -> None:
        self._model = model
        self._budget = budget
        self._identity = getattr(model, "cache_identity", None)
        self._guarded = hasattr(model, "bind_request_guard")
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
        return tuple(getattr(self._model, "last_attempt_records", ()))

    async def invoke(self, schema, system, payload):
        self._check_identity()
        if not self._guarded:
            await self._budget.admit(self._model, schema, system, payload)
        return await self._model.invoke(schema, system, payload)

    async def invoke_validated(self, schema, system, payload, validator):
        self._check_identity()
        method = getattr(self._model, "invoke_validated", None)
        if method is None:
            result = await self.invoke(schema, system, payload)
            validator(result)
            return result
        if not self._guarded:
            await self._budget.admit(self._model, schema, system, payload)
        return await method(schema, system, payload, validator)
