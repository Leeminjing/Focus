"""本文件对外提供真实承诺子图批准到父 checkpoint、故障恢复及双 Provider 请求的集成验证。

输入为确定性 delegator、实际 Lead 工厂、隔离内存／PostgreSQL checkpointer 和无网络 SDK transport；输出为一次交付、冻结来源、请求审计和流隔离断言。
具体工作流为驱动四个既有批准节点，在父持久化前后注入失败，重新构图恢复，再核对精确旧 checkpoint 和实际 Provider payload。
示例：pytest backend/tests/test_commitment_handoff_recovery.py；数据库用现有隔离 fixture，不触及用户 Context。
"""

import asyncio
from copy import deepcopy
import json
import os
import uuid

import httpx
import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

import focus.agents.lead.agent as lead
from backend.tests.runtime_context_support import runtime_context
from backend.tests.test_commitment_resume import _ScriptedDelegator
from focus.agents.commitment.middleware import CommitmentMiddleware
from focus.agents.commitment.workflow import _build_supervisor
from focus.history.bridge import synchronize_items
from focus.models.provider_contract import ProviderContract
from focus.models.responses import FocusResponsesChatModel
from focus.runtime.runs.events import chunk_to_events, serialize_value


def _model(provider, captured):
    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        response = {"object": "response", "id": "fixture-response", "created_at": 0,
            "model": "fixture", "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "message", "id": "answer", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "MAIN_RESULT", "annotations": []}]}]}
        if payload.get("stream"):
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**response["output"][0], "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "item_id": "answer", "output_index": 0, "content_index": 0, "delta": "MAIN_RESULT"},
                {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
                {"type": "response.completed", "response": response},
            ]
            content = "".join("data: " + json.dumps({**event, "sequence_number": n}) + "\n\n" for n, event in enumerate(events))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        return httpx.Response(200, json=response)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
        provider_contract=ProviderContract(provider, "responses"), http_async_client=client), client


def _middleware(delegator):
    middleware = CommitmentMiddleware.__new__(CommitmentMiddleware)
    middleware._skill_names = frozenset()
    middleware._supervisor = _build_supervisor(delegator)
    return middleware


async def _graph(monkeypatch, saver, provider, captured, delegator, extra=()):
    model, client = _model(provider, captured)
    monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
    graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base",
                                       middlewares=[_middleware(delegator)], attempt_middleware=extra[0] if extra else None)
    graph.checkpointer = saver
    return graph, model, client


async def _drive_to_contract_review(graph, config, context):
    result = await graph.ainvoke({"messages": [HumanMessage(id="source-input", content="/commit 做X")]},
                                config, context=context, durability="sync")
    stages = []
    while result.get("__interrupt__"):
        stages.append(result["__interrupt__"][0].value["stage"])
        if stages[-1] == 7:
            break
        result = await graph.ainvoke(Command(resume={"decision": "approve"}), config, context=context, durability="sync")
    assert stages == [3, 5, 6, 7]
    return await graph.aget_state(config)


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_actual_lead_contract_handoff_freezes_knowledge_and_audits_wire(monkeypatch, tmp_path, provider):
    async def run():
        saver, captured, delegator = InMemorySaver(), [], _ScriptedDelegator()
        graph, model, client = await _graph(monkeypatch, saver, provider, captured, delegator)
        config = {"configurable": {"thread_id": "contract-wire-" + uuid.uuid4().hex}}
        context = runtime_context(agent_id="main:contract", task_id="contract", workspace=str(tmp_path),
                                  permissions=("read",), run_id="contract-run")
        try:
            before = await _drive_to_contract_review(graph, config, context)
            original = deepcopy(before.values)
            (tmp_path / "knowledge" / "LangGraph-latest-stable.md").unlink()
            events = []
            async for mode, chunk in graph.astream(Command(resume={"decision": "approve"}), config,
                context=context, stream_mode=["messages", "values", "custom"], durability="sync"):
                events.extend(chunk_to_events(mode, chunk, {"workspace_id": "w", "thread_id": "t", "agent_id": "main", "run_id": "r"}))
            completed = await graph.aget_state(config)
            values = completed.values
            assert values["messages"][0].id == "source-input" and values["messages"][0].content == "/commit 做X"
            contracts = [item for item in values["execution_items"] if item["kind"] == "task_contract"]
            knowledge = [item for item in values["execution_items"] if item["kind"] == "selected_context"]
            assert len(contracts) == len(knowledge) == 1
            assert contracts[0]["origin"] == "delegated" and contracts[0]["scope"] == "revision"
            assert contracts[0]["item_id"] != "source-input"
            assert any(ref.get("run_id") == "contract-run" for ref in contracts[0]["source_refs"])
            assert "正文" in knowledge[0]["payload"]["message"]["content"]
            assert len(captured) == 1
            payload = captured[0]
            assert all(item.get("role") != "developer" for item in payload["input"] if "任务合同" in str(item))
            assert "multi_agent" not in payload and "betas" not in payload
            assert any(item.get("role") == "user" and item.get("content") == values["task_contract"] for item in payload["input"])
            proof = next(binding for binding in values["request_manifest"]["source_bindings"] if binding["kind"] == "task_contract")
            assert proof["source_refs"] == contracts[0]["source_refs"]
            from focus.security.context import security_context_of
            assert security_context_of(context).authorization.permissions == ("read",)
            synchronize_items(values["execution_items"], values["messages"])
            assert (await graph.aget_state(before.config)).values == original
            tokens = [event.data["data"]["content"] for event in events if event.event == "tokens"]
            assert all("正文" not in token and "任务合同" not in token for token in tokens)
            display = serialize_value(values)["messages"]
            assert sum(row["id"] == "source-input" for row in display) == 1
            assert sum("theoretical foundation" in str(row["content"]) for row in display) == 1
            assert delegator.calls == list(range(1, 8))
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["before_parent_commit", "after_parent_commit"])
def test_completed_child_recovers_parent_handoff_once(monkeypatch, tmp_path, failure):
    async def run():
        class Saver(InMemorySaver):
            failed = False

            async def aput(self, config, checkpoint, metadata, new_versions):
                identity = config["configurable"]["thread_id"]
                parent = identity.startswith("handoff-parent-") and not identity.endswith(":commitment")
                ready = checkpoint["channel_values"].get("task_contract")
                if parent and ready and not self.failed:
                    self.failed = True
                    if failure == "after_parent_commit":
                        await super().aput(config, checkpoint, metadata, new_versions)
                    raise RuntimeError("handoff parent checkpoint failure")
                return await super().aput(config, checkpoint, metadata, new_versions)
        saver, captured, delegator = Saver(), [], _ScriptedDelegator()
        graph, model, client = await _graph(monkeypatch, saver, "openai", captured, delegator)
        config = {"configurable": {"thread_id": "handoff-parent-" + uuid.uuid4().hex}}
        context = runtime_context(agent_id="main:recover", task_id="recover", workspace=str(tmp_path),
                                  permissions=("read", "write"), run_id="recover-run")
        try:
            before = await _drive_to_contract_review(graph, config, context)
            with pytest.raises(RuntimeError, match="parent checkpoint failure"):
                await graph.ainvoke(Command(resume={"decision": "approve"}), config, context=context, durability="sync")
            assert not captured
            child = await saver.aget_tuple({"configurable": {"thread_id": config["configurable"]["thread_id"] + ":commitment"}})
            assert child.checkpoint["channel_values"]["stage"] == 9
            reconstructed, second_model, second_client = await _graph(monkeypatch, saver, "openai", captured, delegator)
            try:
                recovered = await reconstructed.ainvoke(None, config, context=context, durability="sync")
                assert sum(item["kind"] == "task_contract" for item in recovered["execution_items"]) == 1
                assert sum(item["kind"] == "selected_context" for item in recovered["execution_items"]) == 1
                assert delegator.calls == list(range(1, 8)) and len(captured) == 1
                synchronize_items(recovered["execution_items"], recovered["messages"])
                assert not (await reconstructed.aget_state(before.config)).values.get("task_contract")
            finally:
                await second_client.aclose()
                second_model._client.close()
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_durable_parent_handoff_recovers_after_commit_before_sampling(monkeypatch, tmp_path):
    async def run():
        async with AsyncPostgresSaver.from_conn_string(os.environ["FOCUS_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")) as saver:
            class FailBeforeSampling(AgentMiddleware):
                def __init__(self, fail=True):
                    self._fail = fail

                async def abefore_model(self, state, runtime):
                    if self._fail:
                        raise RuntimeError("durable handoff before sampling")
            captured, delegator = [], _ScriptedDelegator()
            graph, model, client = await _graph(monkeypatch, saver, "openai", captured, delegator, (FailBeforeSampling(),))
            config = {"configurable": {"thread_id": "durable-contract-" + uuid.uuid4().hex}}
            context = runtime_context(agent_id="main:durable", task_id="durable", workspace=str(tmp_path),
                                      permissions=("read", "write"), run_id="durable-run")
            try:
                await _drive_to_contract_review(graph, config, context)
                with pytest.raises(RuntimeError, match="before sampling"):
                    await graph.ainvoke(Command(resume={"decision": "approve"}), config, context=context, durability="sync")
                saved = await graph.aget_state(config)
                assert sum(item["kind"] == "task_contract" for item in saved.values["execution_items"]) == 1
                assert not captured
                rebuilt, second_model, second_client = await _graph(monkeypatch, saver, "openai", captured, delegator, (FailBeforeSampling(False),))
                try:
                    result = await rebuilt.ainvoke(None, config, context=context, durability="sync")
                    assert sum(item["kind"] == "task_contract" for item in result["execution_items"]) == 1
                    assert len(captured) == 1 and delegator.calls == list(range(1, 8))
                    assert saved.values == (await rebuilt.aget_state(saved.config)).values
                finally:
                    await second_client.aclose()
                    second_model._client.close()
            finally:
                await client.aclose()
                model._client.close()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
