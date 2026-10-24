r"""本文件对外提供 IndexModelBudget 与 BudgetedIndexModel。

输入为冻结资源策略、模型窗口和结构化 payload；输出为 attempt 准入、纯窗口预检或显式预算异常。
具体工作流为按完整请求 UTF-8 字节上界估计输入，预留输出／调用；生产模型绑定逐次 owner receipt，局部窗口检查不重复预留；兼容未托管模型逐次预留，结算后同步释放本地差额。
resources 提供冻结策略；fits_request/require_fit 纯检查容量，后者拒绝时提供安全尺寸诊断；admit 才为局部／综合／verifier attempts 预留额度。未知崩溃预留仍占额度。
fits_available_budget 为可选原文预取检查窗口与本轮剩余额度，不预留或消费；实际发送仍由 admit 和持久授权准入重验。
BudgetedIndexModel.last_attempt_records 只包含本次 invocation：准入／配置拒绝返回空记录，已发送的失败或取消保留真实 attempts。
可选共享 semaphore 限制完整 invocation（含内部重试），排队取消不准入、不重复捕获旧 attempt。
运行日志分别记录排队、预算准入和执行耗时，包含失败与取消，不记录原文或凭据。
容量拒绝提供请求 schema、hash 和字段字节尺寸的安全诊断，不保存正文或凭据，也不凭通用超窗推断 Tool 原子过大。
示例：model.fits_request(schema, prompt, multi_segment_payload)；多段原文超窗显式阻断，Tool 原子段不切开。
"""

import asyncio
import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from hashlib import sha256
from time import perf_counter_ns
from focus.models.telemetry import model_invocation, model_context, emit

from backend.app.desktop.patrol.agent_loop.resources.resource_limits import (
    exceeds_limit,
    remaining_capacity,
    request_limits,
)
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta

_logger = logging.getLogger(__name__)


class IndexBudgetExceeded(ValueError):
    def __init__(self, message, *, request_diagnostic=None):
        super().__init__(message)
        self.request_diagnostic = request_diagnostic


def index_failure_progress_status(message):
    if "provider_request_window" in message:
        return "capacity_blocked"
    return "blocked" if "exhausted" in message else "failed"


@dataclass
class _LocalReservation:
    amounts: list[int] = field(default_factory=lambda: [0, 0, 0])
    receipt: object | None = None
    settled: bool = False


class _BudgetUsageReceipts:
    """Local and durable accounting share the same per-request lifecycle."""

    def __init__(self, budget, receipts):
        self._budget = budget
        self._receipts = receipts

    async def reserve(self, input_tokens, output_tokens, *, retry=False):
        budget = self._budget
        output_tokens = max(output_tokens, budget.resources.policy.output_token_reserve or 0)
        async with budget._lock:
            budget._check_available(input_tokens, output_tokens)
            receipt = await self._receipts.reserve(input_tokens, output_tokens, retry=retry)
            reservation = _LocalReservation([1, input_tokens, output_tokens], receipt)
            budget._occupy(reservation.amounts)
            return reservation

    async def settle(self, reservation, measured, reported):
        async with self._budget._lock:
            if reservation.settled:
                return
            # Never refund locally before durable settlement succeeds. Unknown
            # receipts retain their entire reservation, including after cancel.
            await self._receipts.settle(reservation.receipt, measured, reported)
            if reported:
                self._budget._reconcile(reservation, measured)


class IndexModelBudget:
    def __init__(self, resources, *, reservations=None, usage_receipts=None) -> None:
        self._resources = resources
        self._calls = resources.global_model_calls_remaining
        self._input = resources.global_input_tokens_remaining
        self._output = resources.global_output_tokens_remaining
        self._lock = asyncio.Lock()
        self._reservations = reservations
        self._occupied = [0, 0, 0]
        self._batch = _LocalReservation(receipt=reservations)
        self.usage_receipts = _BudgetUsageReceipts(self, usage_receipts) if usage_receipts is not None else None

    @property
    def has_reservations(self) -> bool:
        return self._reservations is not None

    @property
    def resources(self):
        return self._resources

    async def settle(self, usage, *, reported=True) -> None:
        await self._settle_reservation(self._batch, usage, reported=reported)

    async def _settle_reservation(self, reservation, usage, *, reported):
        async with self._lock:
            if reservation.settled or not any(reservation.amounts):
                return
            if reservation.receipt is not None:
                if reported:
                    await reservation.receipt.settle(usage)
                else:
                    await reservation.receipt.mark_unknown()
            if reported:
                self._reconcile(reservation, usage)

    def _check_available(self, estimated, output):
        if exceeds_limit(1, self._calls) or exceeds_limit(estimated, self._input) or exceeds_limit(output, self._output):
            raise IndexBudgetExceeded("semantic index authorized_model_budget exhausted")

    def _occupy(self, amounts):
        # Retain overages even when the displayed remaining capacity is zero;
        # settling another request must not erase actual consumption above a cap.
        self._occupied = [used + amount for used, amount in zip(self._occupied, amounts, strict=True)]
        self._calls, self._input, self._output = (
            remaining_capacity(limit, used)
            for limit, used in zip((self._resources.global_model_calls_remaining,
                                   self._resources.global_input_tokens_remaining,
                                   self._resources.global_output_tokens_remaining), self._occupied, strict=True)
        )

    def _reconcile(self, reservation, usage):
        actual = [max(0, amount) for amount in (usage.model_calls, usage.input_tokens, usage.output_tokens)]
        self._occupy([used - reserved for used, reserved in zip(actual, reservation.amounts, strict=True)])
        reservation.settled = True

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

    def fits_available_budget(self, model, schema, system, payload):
        estimated = self._estimated_input(schema, system, payload)
        limit, output = self._request_limits(model)
        return not (
            exceeds_limit(estimated, limit)
            or exceeds_limit(estimated, self._input)
            or exceeds_limit(1, self._calls)
            or exceeds_limit(output, self._output)
        )

    async def admit(self, model, schema, system, payload, *, per_attempt=False):
        estimated, _, output = self.require_fit(model, schema, system, payload)
        async with self._lock:
            self._check_available(estimated, output)
            if getattr(model, "usage_managed", False) and self.usage_receipts is not None:
                # The bound receipt reserves and settles both budgets around the
                # actual send, including each retry inside one invocation.
                return None
            if per_attempt:
                receipt = self._reservations.new_request() if self._reservations is not None else None
                reservation = _LocalReservation(receipt=receipt)
            else:
                if self._batch.settled and self._reservations is None:
                    self._batch = _LocalReservation()
                reservation = self._batch
            if reservation.receipt is not None:
                await reservation.receipt.reserve(estimated, output)
            amounts = [1, estimated, output]
            reservation.amounts = [old + amount for old, amount in zip(reservation.amounts, amounts, strict=True)]
            self._occupy(amounts)
            return reservation

    def require_fit(self, model, schema, system, payload):
        estimated = self._estimated_input(schema, system, payload)
        limit, output = self._request_limits(model)
        if exceeds_limit(estimated, limit):
            raise IndexBudgetExceeded(
                f"semantic index provider_request_window exceeded: input_bound={estimated}, input_limit={limit}, output_reserve={output}; choose a smaller complete original window",
                request_diagnostic=self._capacity_diagnostic(schema, system, payload, estimated, limit, output),
            )
        return estimated, limit, output

    @staticmethod
    def _capacity_diagnostic(schema, system, payload, estimated, limit, output):
        encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = {"system": system, "payload": payload, "schema": schema.model_json_schema()}
        return {"schema": schema.__name__, "request_hash": sha256(encode(request)).hexdigest(),
                "input_bound": estimated, "input_limit": limit, "output_reserve": output,
                "payload_field_bytes": {key: len(encode(value)) for key, value in payload.items()},
                "system_bytes": len(system.encode("utf-8")), "schema_bytes": len(encode(request["schema"]))}


class BudgetedIndexModel:
    def __init__(
        self, model, budget: IndexModelBudget, *, semaphore: asyncio.Semaphore | None = None,
        telemetry_context=None,
        progress=None,
        background=False,
    ) -> None:
        self._model = model
        self._budget = budget
        self._semaphore = semaphore
        self._admission_ms = 0.0
        self._telemetry_context = dict(telemetry_context or {})
        self._identity = getattr(model, "cache_identity", None)
        self._output_token_limit = getattr(model, "max_output_tokens", None)
        self._guarded = hasattr(model, "bind_request_guard")
        self._last_attempt_records = ()
        self._progress = progress
        self._background = background
        self._admissions = []
        if budget.usage_receipts is not None and hasattr(model, "bind_usage_receipts"):
            model.bind_usage_receipts(budget.usage_receipts)
        if self._guarded:
            model.bind_request_guard(self._admit_checked)

    async def _admit_checked(self, model, schema, system, payload):
        self._check_identity()
        started = perf_counter_ns()
        try:
            reservation = await self._budget.admit(model, schema, system, payload, per_attempt=True)
            if reservation is not None:
                self._admissions.append(reservation)
            await self._report("running")
        finally:
            self._admission_ms += (perf_counter_ns() - started) / 1_000_000

    def _check_identity(self):
        if getattr(self._model, "cache_identity", None) != self._identity:
            raise ValueError("semantic index model configuration 已 stale")

    @property
    def last_attempt_records(self):
        return self._last_attempt_records

    def fits_request(self, schema, system, payload):
        self._check_identity()
        return self._budget.fits_request(self._model, schema, system, payload)

    def fits_available_budget(self, schema, system, payload):
        self._check_identity()
        return self._budget.fits_available_budget(self._model, schema, system, payload)

    def require_fit(self, schema, system, payload):
        self._check_identity()
        return self._budget.require_fit(self._model, schema, system, payload)

    async def invoke(self, schema, system, payload):
        return await self._invoke(schema, system, payload)

    async def invoke_validated(self, schema, system, payload, validator):
        return await self._invoke(schema, system, payload, validator)

    async def _invoke(self, schema, system, payload, validator=None):
        self._last_attempt_records = ()
        category = "index_prewarm" if self._background else "index_foreground"
        with model_invocation(category=category, **self._telemetry_context):
            state = {"entered": False}
            task = asyncio.create_task(self._invoke_observed(schema, system, payload, validator, state))
            try:
                # Catch caller cancellation before propagating it into model/budget cleanup.
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled_at = perf_counter_ns()
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve any cleanup exception without replacing caller cancellation.
                if not task.cancelled():
                    task.exception()
                emit("index_cancelled", **(model_context.get() or {}), queued=not state["entered"],
                     origin="caller" if asyncio.current_task().cancelling() else "child",
                     release_ms=((perf_counter_ns() - cancelled_at) / 1_000_000
                                 if asyncio.current_task().cancelling() else None))
                raise

    async def _invoke_observed(self, schema, system, payload, validator=None, cancellation_state=None):
        self._last_attempt_records = ()
        self._admissions = []
        self._admission_ms = 0.0
        started = perf_counter_ns()
        entered = None
        outcome = "error"
        try:
            waiting = (self._semaphore.would_wait(background=self._background)
                       if hasattr(self._semaphore, "would_wait") else
                       self._semaphore is not None and self._semaphore.locked())
            if waiting:
                await self._report("queued")
            slot = (self._semaphore.slot(background=self._background)
                    if hasattr(self._semaphore, "slot") else self._semaphore)
            async with slot if slot is not None else nullcontext():
                entered = perf_counter_ns()
                if cancellation_state is not None:
                    cancellation_state["entered"] = True
                result = await self._invoke_with_slot(schema, system, payload, validator)
                outcome = "ready"
                return result
        except IndexBudgetExceeded as exc:
            outcome = "blocked"
            await self._report(index_failure_progress_status(str(exc)))
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            finished = perf_counter_ns()
            metrics = {
                **self._telemetry_context,
                "schema_name": schema.__name__, "invocation_outcome": outcome,
                "queue_wait_ms": ((entered or finished) - started) / 1_000_000,
                "execution_ms": (finished - entered) / 1_000_000 if entered else 0.0,
                "admission_ms": self._admission_ms,
                "output_token_limit": self._output_token_limit,
                "model_calls": sum(int(a.get("model_calls") or 0) for a in self._last_attempt_records),
                "input_tokens": sum(int(a.get("input_tokens") or 0) for a in self._last_attempt_records),
                "output_tokens": sum(int(a.get("output_tokens") or 0) for a in self._last_attempt_records),
            }
            _logger.info("semantic_index_model_invocation %s", metrics, extra=metrics)
            emit("index_invocation", **{**(model_context.get() or {}), **metrics})

    async def _report(self, status):
        if self._progress is not None:
            await self._progress(status=status, **self._telemetry_context)

    async def _invoke_with_slot(self, schema, system, payload, validator):
        self._check_identity()
        method = (
            getattr(self._model, "invoke_validated", None)
            if validator is not None
            else None
        )
        if not self._guarded:
            await self._admit_checked(self._model, schema, system, payload)
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
            await self._settle_admissions()

    async def _settle_admissions(self):
        for position, reservation in enumerate(self._admissions):
            # Models without a per-attempt guard may report several attempts for
            # their single invocation reservation. Bill all of them together.
            attempts = self._last_attempt_records[position:position + 1] if self._guarded else self._last_attempt_records
            usage = LoopUsageDelta(
                **{name: sum(max(0, int(item.get(name) or 0)) for item in attempts)
                   for name in ("model_calls", "input_tokens", "output_tokens")},
                retries=sum(int(item.get("attempt", 1) > 1) for item in attempts),
            )
            await asyncio.shield(self._budget._settle_reservation(
                reservation, usage, reported=bool(attempts) and all(item.get("usage_reported", True) for item in attempts),
            ))
            if reservation.receipt is not None:
                for attempt in attempts:
                    attempt["usage_managed"] = True
