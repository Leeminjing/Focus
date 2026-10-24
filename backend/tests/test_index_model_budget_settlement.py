"""索引预算按实际消费和在途／未知预留记账的回归测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from backend.app.desktop.patrol.agent_loop.resources.budget_presets import initial_budgets
from backend.app.desktop.patrol.agent_loop.context_expansion.index_model_budget import (
    BudgetedIndexModel,
    IndexBudgetExceeded,
    IndexModelBudget,
)
from backend.app.desktop.patrol.agent_loop.workers.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.patrol.agent_loop.resources.expansion_resource_policy import resolve_expansion_resources
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta
from backend.tests.config_helpers import app_config_for


class Answer(BaseModel):
    answer: str


class Receipts:
    """只替换耐久存储；使用生产模型调用和用量报告路径。"""

    def __init__(self):
        self.rows = []

    async def reserve(self, input_tokens, output_tokens, *, retry=False):
        row = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              measured=None, reported=False)
        self.rows.append(row)
        return row

    async def settle(self, receipt, measured, reported):
        receipt.measured, receipt.reported = measured, reported


def test_normal_budget_releases_each_reported_production_attempt(monkeypatch):
    async def run():
        class Provider:
            async def ainvoke(self, messages, config):
                config["callbacks"][0].usage_metadata["budget-test"] = {
                    "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                }
                return AIMessage(content=json.dumps({"answer": "ok"}))

        monkeypatch.setattr("backend.app.desktop.patrol.agent_loop.workers.structured_worker.create_chat_model",
                            lambda **kwargs: Provider())
        config = app_config_for("budget-test", None)
        config.models[0].curation_output_method = "prompt_json"
        receipts = Receipts()
        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1),
                                  usage_receipts=receipts)
        model = RoleBoundStructuredModel(config, "semantic_index_projector")
        assert model.max_output_tokens == 8192
        wrapped = BudgetedIndexModel(model, budget)
        for _ in range(7):
            await wrapped.invoke(Answer, "Return an answer", {})
        assert len(receipts.rows) == 7
        assert budget._output == 50_000 - 7
        assert budget._input == 500_000 - 7
        assert budget._calls == 100 - 7

    asyncio.run(run())


@pytest.mark.parametrize("validation_failure", [False, True])
def test_each_reported_retry_is_released_before_the_next_attempt(monkeypatch, validation_failure):
    async def run():
        calls = 0

        class Provider:
            async def ainvoke(self, messages, config):
                nonlocal calls
                calls += 1
                config["callbacks"][0].usage_metadata["budget-test"] = {
                    "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                }
                body = {"answer": "ok"} if validation_failure or calls == 7 else {"invalid": True}
                return AIMessage(content=json.dumps(body))

        monkeypatch.setattr("backend.app.desktop.patrol.agent_loop.workers.structured_worker.create_chat_model",
                            lambda **kwargs: Provider())
        config = app_config_for("budget-test", None)
        config.models[0].curation_output_method = "prompt_json"
        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1), usage_receipts=Receipts())
        wrapped = BudgetedIndexModel(RoleBoundStructuredModel(config, "semantic_index_projector", max_attempts=7), budget)

        def validate(result):
            if validation_failure and calls < 7:
                raise ValueError("try again")

        await wrapped.invoke_validated(Answer, "Return an answer", {}, validate)
        assert calls == len(wrapped.last_attempt_records) == 7
        assert budget._output == 49_993

    asyncio.run(run())


def test_batch_settlement_releases_only_once():
    async def run():
        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1))
        model = SimpleNamespace(max_output_tokens=8192)
        for _ in range(6):
            await budget.admit(model, Answer, "test", {})
        assert budget._output == 848
        usage = LoopUsageDelta(model_calls=6, input_tokens=6, output_tokens=6)
        await asyncio.gather(budget.settle(usage), budget.settle(usage))
        assert budget._output == 49_994
        await budget.admit(model, Answer, "test", {})

    asyncio.run(run())


def test_unguarded_model_bills_all_attempts_for_its_invocation():
    async def run():
        class Model:
            last_attempt_records = ()

            async def invoke(self, schema, system, payload):
                self.last_attempt_records = tuple(
                    {"model_calls": 1, "input_tokens": 2, "output_tokens": 3, "attempt": number}
                    for number in range(1, 4)
                )
                return schema(answer="ok")

        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1))
        await BudgetedIndexModel(Model(), budget).invoke(Answer, "test", {})
        assert (budget._calls, budget._input, budget._output) == (97, 499_994, 49_991)

    asyncio.run(run())


def test_pending_and_unknown_receipts_keep_their_own_reservations():
    async def run():
        receipts = Receipts()
        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1), usage_receipts=receipts)
        tracked = budget.usage_receipts
        pending, unknown, done = await asyncio.gather(*(tracked.reserve(1000, 8192) for _ in range(3)))
        actual = LoopUsageDelta(model_calls=1, input_tokens=1, output_tokens=1)
        await tracked.settle(unknown, actual, False)
        await asyncio.gather(tracked.settle(done, actual, True), tracked.settle(done, actual, True))
        assert (budget._calls, budget._input, budget._output) == (97, 497_999, 33_615)
        await tracked.settle(pending, actual, True)
        assert budget._output == 50_000 - 8192 - 2
        # A late reliable report may replace an unknown reservation, once.
        await tracked.settle(unknown, actual, True)
        assert budget._output == 49_997

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["reserve", "settle"])
def test_persistent_failure_does_not_refund_or_double_charge(failure):
    async def run():
        class FailingReceipts(Receipts):
            async def reserve(self, *args, **kwargs):
                if failure == "reserve":
                    raise RuntimeError("database failure")
                return await super().reserve(*args, **kwargs)

            async def settle(self, *args):
                raise RuntimeError("database failure")

        budget = IndexModelBudget(resolve_expansion_resources(initial_budgets(), 1), usage_receipts=FailingReceipts())
        if failure == "reserve":
            with pytest.raises(RuntimeError, match="database failure"):
                await budget.usage_receipts.reserve(1000, 8192)
            assert budget._output == 50_000
        else:
            receipt = await budget.usage_receipts.reserve(1000, 8192)
            with pytest.raises(RuntimeError, match="database failure"):
                await budget.usage_receipts.settle(receipt, LoopUsageDelta(model_calls=1, output_tokens=1), True)
            assert budget._output == 50_000 - 8192

    asyncio.run(run())


def test_concurrent_admission_is_bounded_and_settlement_allows_next_call():
    async def run():
        limits = {**initial_budgets(), "max_output_tokens": 8192}
        budget = IndexModelBudget(resolve_expansion_resources(limits, 1), usage_receipts=Receipts())
        results = await asyncio.gather(*(budget.usage_receipts.reserve(1000, 8192) for _ in range(2)),
                                       return_exceptions=True)
        assert sum(isinstance(result, IndexBudgetExceeded) for result in results) == 1
        receipt = next(result for result in results if not isinstance(result, Exception))
        await budget.usage_receipts.settle(receipt, LoopUsageDelta(model_calls=1, output_tokens=1), True)
        with pytest.raises(IndexBudgetExceeded):
            await budget.usage_receipts.reserve(1000, 8192)
        await budget.usage_receipts.reserve(1000, 8191)

    asyncio.run(run())


def test_explicit_output_reserve_is_kept_in_both_budgets():
    async def run():
        limits = {**initial_budgets(), "max_output_tokens": 19_000,
                  "expansion_resources": {"output_token_reserve": 10_000}}
        receipts = Receipts()
        budget = IndexModelBudget(resolve_expansion_resources(limits, 1), usage_receipts=receipts)
        first = await budget.usage_receipts.reserve(100, 8192)
        assert budget._output == 9000 and receipts.rows[0].output_tokens == 10_000
        with pytest.raises(IndexBudgetExceeded):
            await budget.usage_receipts.reserve(100, 8192)
        await budget.usage_receipts.settle(first, LoopUsageDelta(model_calls=1, output_tokens=1), True)
        await budget.usage_receipts.reserve(100, 8192)
        assert budget._output == 8999

    asyncio.run(run())


@pytest.mark.parametrize("unlimited", [False, True])
def test_refund_preserves_frozen_usage_unlimited_and_actual_overruns(unlimited):
    async def run():
        limits = initial_budgets()
        if unlimited:
            limits.update(max_model_calls=None, max_input_tokens=None, max_output_tokens=None)
        budget = IndexModelBudget(resolve_expansion_resources(limits, 1,
                                  {"model_calls": 2, "input_tokens": 30, "output_tokens": 20}), usage_receipts=Receipts())
        a = await budget.usage_receipts.reserve(1000, 8192)
        b = await budget.usage_receipts.reserve(1000, 8192)
        await budget.usage_receipts.settle(a, LoopUsageDelta(model_calls=1, input_tokens=1, output_tokens=60_000), True)
        await budget.usage_receipts.settle(b, LoopUsageDelta(model_calls=1, input_tokens=1, output_tokens=1), True)
        if unlimited:
            assert budget._calls is budget._input is budget._output is None
        else:
            assert (budget._calls, budget._input, budget._output) == (96, 499_968, 0)
            assert not budget.fits_available_budget(SimpleNamespace(max_output_tokens=8192), Answer, "test", {})

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("production_model", [False, True])
def test_normal_budget_full_index_cold_append_and_cache(tmp_path, monkeypatch, production_model):
    from sqlalchemy import select

    from backend.app.desktop.patrol.agent_loop.context_expansion.interpretation_record import RevisionInterpretationProposal
    from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopIndexBudgetReservation
    from backend.app.desktop.patrol.agent_loop.context_expansion.portfolio_index import PortfolioSemanticIndexService
    from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_grounding import (
        SemanticClaimSupportProposal, SemanticProjectionProposal,
    )
    from backend.app.desktop.patrol.agent_loop.models import LoopBudgetUsage
    from backend.tests.incremental_index_support import Checkpoints, ModelProbe, messages, observation, revision
    from backend.tests.test_incremental_index_real_model import observed_model_factory
    from backend.tests.test_incremental_revision_index import exercise

    budgets = initial_budgets()

    async def run(sessions, seed):
        probe = ModelProbe()
        attempts = []
        if production_model:
            config = app_config_for("budget-test", None)
            config.models[0].curation_output_method = "prompt_json"

            class Provider:
                async def ainvoke(self, request, config):
                    content = request[-1].content
                    payload = json.loads(content.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])
                    if "inventory" in payload:
                        schema, role = RevisionInterpretationProposal, "projector"
                    elif "claims" in payload:
                        schema, role = SemanticClaimSupportProposal, "verifier"
                    else:
                        schema, role = SemanticProjectionProposal, "projector"
                    result = await probe.factory(role)().invoke(schema, "test", payload)
                    config["callbacks"][0].usage_metadata["budget-test"] = {
                        "input_tokens": 40, "output_tokens": 10, "total_tokens": 50,
                    }
                    return AIMessage(content=result.model_dump_json())

            monkeypatch.setattr("backend.app.desktop.patrol.agent_loop.workers.structured_worker.create_chat_model",
                                lambda **kwargs: Provider())
            service = PortfolioSemanticIndexService(sessions, Checkpoints(),
                semantic_projector_factory=observed_model_factory(config, "semantic_index_projector", [], attempts),
                semantic_claim_verifier_factory=observed_model_factory(config, "claim_verifier", [], attempts))
        else:
            service = probe.service(sessions)
        history = messages(37)
        first = await revision(sessions, seed["context_id"], history)
        cold = await service.build(observation(seed, first, budget=budgets))
        assert cold.blocker_code is None, cold.blocker_summary
        assert cold.indexes[0].quality_state == "complete" and len(probe.calls) > 6
        target = await revision(sessions, seed["context_id"], history + messages(1, 37), parent=first)
        appended = await service.build(observation(seed, target, budget=budgets))
        assert appended.blocker_code is None, appended.blocker_summary
        assert appended.indexes[0].inheritance.reused_segment_ids
        count = len(probe.calls)
        cached = await service.build(observation(seed, target, budget=budgets))
        assert cached.indexes == appended.indexes and len(probe.calls) == count
        async with sessions() as session:
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            rows = (await session.scalars(select(LoopIndexBudgetReservation).where(
                LoopIndexBudgetReservation.loop_id == seed["loop_id"]))).all()
            assert usage.model_calls == count == len(rows)
            assert usage.output_tokens == count * 10 < budgets["max_output_tokens"]
            assert all(row.settled_at is not None for row in rows)
            assert sum(row.actual_usage["input_tokens"] for row in rows) == usage.input_tokens
            assert usage.input_tokens < budgets["max_input_tokens"]

    exercise(tmp_path, run, budgets=budgets)
