"""
承诺层（commitment-layer）PoC 测试。

覆盖:
    开关与装配 — build_general_middlewares 按 commitment.enabled 条件装配
    触发解析 — /commit 显式触发、前导 skill token 剥离、普通/空/中部命令不触发
    九阶段状态机 — Supervisor 路由、阶段信封、stage 严格递增
    Worker–Evaluator 审核闭环 — 最多三次重试、结构失败合成审核结果
    人工修订 — approve/revise 语义、replacement 重审、失败草稿不可批准
    消息账本 — 阶段结果提取、原位替换、账本纯净
    Context7 解析 — library_id / stable version / candidate version / evidence
    阶段规则 — 阶段2/3/4 语义校验、阶段3 逐字继承、阶段4 引用过滤
    interrupt/resume — Interrupt 序列化提取、middleware 首次与恢复分支
"""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from focus.agents.commitment import (
    CommitmentMiddleware,
    CommitmentState,
    TaskEnvelope,
    WorkerOutput,
    _build_supervisor,
    _commit_instruction,
    _context7_candidate_version,
    _context7_stable_version,
    _filter_stage_four_result,
    _human_payload,
    _normalize_stage_three_result,
    _safe_segment,
    _stage_four_needs_review,
    _validate_stage_result,
)
from focus.agents.commitment.stage_rules import _context7_library_id
from focus.agents.commitment.middleware import (
    _extract_uploads_tag,
    _strip_leading_skill_tokens,
)
from focus.agents.commitment.stage_rules import (
    _extract_structured,
    _has_open_conflicts,
    _stage_envelope,
)
from focus.agents.lead.middlewares import build_general_middlewares
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import extract_interrupts, serialize_interrupt
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Interrupt


def _app_config(enabled: bool = False) -> AppConfig:
    return AppConfig.model_validate({
        "models": [{
            "name": "test-model", "display_name": "Test", "use": "langchain_openai:ChatOpenAI",
            "model": "test-model", "api_key": "k", "base_url": "http://x",
        }],
        "commitment": {"enabled": enabled},
    })


# === 1. 配置与装配 ===

def test_builder_disabled_returns_empty_chain():
    middlewares = build_general_middlewares(_app_config(False), skill_names=frozenset({"docx"}))
    assert middlewares == []


def test_builder_enabled_assembles_commitment_middleware():
    async def load_context7_tools():
        return [object()]

    middlewares = build_general_middlewares(
        _app_config(True),
        model=_FakeModel([], []),
        context7_tools_loader=load_context7_tools,
        skill_names=frozenset({"docx"}),
    )
    assert len(middlewares) == 1
    assert isinstance(middlewares[0], CommitmentMiddleware)
    assert middlewares[0]._skill_names == frozenset({"docx"})


def test_builder_enabled_rejects_missing_context7_loader():
    with pytest.raises(RuntimeError, match="Context7 工具加载器"):
        build_general_middlewares(
            _app_config(True),
            model=_FakeModel([], []),
        )


def test_normal_message_does_not_load_context7():
    calls = 0

    async def load_context7_tools():
        nonlocal calls
        calls += 1
        raise RuntimeError("Context7 connection timeout")

    middleware = build_general_middlewares(
        _app_config(True),
        model=_FakeModel([], []),
        context7_tools_loader=load_context7_tools,
    )[0]

    result = asyncio.run(
        middleware.abefore_agent(
            {"messages": [HumanMessage(content="普通会话消息", id="m-normal")]},
            None,
        )
    )

    assert result is None
    assert calls == 0


def test_repository_config_enables_commitment_by_default():
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert config["commitment"]["enabled"] is True


def test_context7_uses_http_bearer_and_shared_loader(monkeypatch):
    import focus.mcp.context7 as context7

    captured = {}
    expected = [object()]

    async def fake_load(servers_config):
        captured.update(servers_config)
        return expected

    monkeypatch.setattr(context7, "load_mcp_tools", fake_load)
    monkeypatch.setenv("CONTEXT7_API_KEY", "secret")
    result = asyncio.run(context7.get_context7_tools("https://context7.example/mcp"))

    assert result is expected
    assert captured == {
        "context7": {
            "transport": "http",
            "url": "https://context7.example/mcp",
            "headers": {"Authorization": "Bearer secret"},
        }
    }


def test_make_lead_agent_defers_context7_until_loader_is_called(monkeypatch):
    import focus.agents.lead.agent as lead_agent
    import focus.agents.lead.middlewares as lead_middlewares
    import focus.mcp as focus_mcp

    from focus.plugins.bridge import PluginBridgeMiddleware

    model = _FakeModel([], [])
    context7_tools = [object()]
    commitment_middleware = object()
    tool_error_middleware = object()
    captured = {}
    expected_graph = object()

    context7_calls = 0

    async def fake_context7(url):
        nonlocal context7_calls
        context7_calls += 1
        captured["url"] = url
        return context7_tools

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return [commitment_middleware]

    def fake_create_agent(**kwargs):
        captured["middleware"] = kwargs["middleware"]
        return expected_graph

    monkeypatch.setattr(lead_agent, "create_chat_model", lambda **_kwargs: model)
    monkeypatch.setattr(lead_agent, "create_agent", fake_create_agent)
    monkeypatch.setattr(focus_mcp, "get_context7_tools", fake_context7)
    monkeypatch.setattr(lead_middlewares, "build_general_middlewares", fake_builder)

    result = asyncio.run(lead_agent.make_lead_agent(
        tools=[],
        system_prompt="prompt",
        app_config=_app_config(True),
        middleware_skill_names=frozenset({"docx"}),
        additional_middlewares=[tool_error_middleware],
    ))

    assert result is expected_graph
    assert context7_calls == 0
    assert captured["model"] is model
    assert callable(captured["context7_tools_loader"])
    assert asyncio.run(captured["context7_tools_loader"]()) is context7_tools
    assert context7_calls == 1
    assert captured["url"] == "https://mcp.context7.com/mcp"
    assert captured["skill_names"] == frozenset({"docx"})
    # 插件桥接位于最终链最末端（既有中间件保持原位置）
    assert captured["middleware"][:2] == [commitment_middleware, tool_error_middleware]
    assert isinstance(captured["middleware"][-1], PluginBridgeMiddleware)


# === 2. 触发解析 ===

def test_commit_instruction_explicit_trigger():
    message = HumanMessage(content="/commit 做X", id="m1")
    assert _commit_instruction(message, frozenset({"docx"})) == "做X"


def test_commit_instruction_strips_leading_skill_tokens():
    message = HumanMessage(content="/docx /commit 做X", id="m1")
    assert _commit_instruction(message, frozenset({"docx"})) == "做X"


def test_commit_instruction_unknown_token_not_stripped():
    message = HumanMessage(content="/unknown /commit 做X", id="m1")
    assert _commit_instruction(message, frozenset({"docx"})) is None


def test_commit_instruction_plain_message_skips():
    for content in ("普通消息", "做 /commit 事", "/commitment 做X", "/commit   ", "/commit"):
        assert _commit_instruction(HumanMessage(content=content, id="m1"), frozenset({"docx"})) is None


def test_commit_instruction_only_checks_last_message():
    assert _commit_instruction(HumanMessage(content="你好", id="m1"), frozenset()) is None


def test_strip_leading_skill_tokens_loop():
    assert _strip_leading_skill_tokens("/docx /pdf /commit 做X", frozenset({"docx", "pdf"})) == "/commit 做X"


def test_extract_uploads_tag_separates_tag():
    clean, tag = _extract_uploads_tag("做X\n\n<current_uploads>\na.md\n</current_uploads>")
    assert clean == "做X"
    assert "<current_uploads>" in tag and "a.md" in tag


# === 3. 状态机 ===

def test_stage_envelope_carries_source_text_and_uploads():
    envelope = _stage_envelope(2, {"source_text": "做X", "uploads_tag": "<current_uploads>a.md</current_uploads>"})
    assert envelope.stage == 2
    assert envelope.context["source_text"] == "做X"
    assert "a.md" in envelope.context["current_uploads"]
    assert envelope.acceptance_criteria


def test_human_review_stages_and_conditional_stages():
    # 阶段 2 有 open conflict → 必须 revise
    payload = _human_payload(2, {
        "artifacts": {"2": {
            "compatibility_checks": [{"status": "conflict"}],
            "conflicts": [{"status": "open"}],
        }}
    })
    assert payload["allowed_decisions"] == ["revise"]
    # 阶段 3 正常 → approve + revise
    payload = _human_payload(3, {"artifacts": {"3": {"requirements": []}}})
    assert payload["allowed_decisions"] == ["approve", "revise"]
    # reviewed_failed → 只能 revise
    payload = _human_payload(3, {"artifacts": {"3": {"status": "reviewed_failed"}}})
    assert payload["allowed_decisions"] == ["revise"]


# === 4. Worker–Evaluator 审核闭环 ===

class _FakeModel:
    """可编程的假模型：按调用轮次返回 Worker/Evaluator 结果。"""

    def __init__(self, worker_results, evaluator_results):
        self.worker_results = list(worker_results)
        self.evaluator_results = list(evaluator_results)
        self.worker_calls = 0
        self.evaluator_calls = 0

    def bind_tools(self, *args, **kwargs):
        return self

    def with_structured_output(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise NotImplementedError


def test_context7_tools_are_loaded_once_on_first_stage_use():
    from focus.agents.commitment import ReviewedDelegator

    calls = 0
    resolver = type("FakeTool", (), {"name": "resolve-library-id"})()

    async def load_context7_tools():
        nonlocal calls
        calls += 1
        return [resolver]

    delegator = ReviewedDelegator(
        _FakeModel([], []),
        context7_tools_loader=load_context7_tools,
    )

    async def exercise():
        assert await delegator._context7_tool("resolve-library-id") is resolver
        assert await delegator._context7_tool("resolve-library-id") is resolver

    asyncio.run(exercise())
    assert calls == 1


def test_commitment_structured_agents_enable_deepseek_json_without_mutating_model(
    monkeypatch,
):
    from langchain_openai import ChatOpenAI
    from focus.agents.commitment import ReviewedDelegator
    import focus.agents.commitment.delegation as delegation_module

    source_model = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        extra_body={"user_id": "commitment-test"},
    )
    delegator = ReviewedDelegator(source_model, context7_tools=[])
    created_models = []

    def fake_create_agent(*, model, **_kwargs):
        created_models.append(model)
        return object()

    async def fake_stream_agent(
        _agent,
        _messages,
        *,
        actor,
        stage,
        stream_id,
        attempt=1,
    ):
        del stage, stream_id, attempt
        content = (
            '{"approved":true,"feedback":"","reasoning_summary":"通过"}'
            if actor == "evaluator"
            else '{"result":{"goal":"X"},"reasoning_summary":"完成"}'
        )
        return {"messages": [AIMessage(content=content)]}

    monkeypatch.setattr(delegation_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(delegator, "_stream_agent", fake_stream_agent)

    envelope = TaskEnvelope(stage=1, instruction="明确目标")
    worker_output = asyncio.run(delegator._worker(envelope, ""))
    review = asyncio.run(delegator._evaluator(envelope, worker_output))
    asyncio.run(
        delegator._invoke_schema(
            name="commitment_schema_probe",
            system_prompt="只返回 JSON",
            prompt={"stage": 1},
            schema=WorkerOutput,
            attempt=1,
        )
    )

    assert review.approved is True
    assert len(created_models) == 3
    for model in created_models:
        tool_bound_model = model.bind_tools([])
        payload = tool_bound_model.bound._get_request_payload([("human", "json")])
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["reasoning_effort"] == "high"
        assert payload["extra_body"] == {
            "user_id": "commitment-test",
            "thinking": {"type": "enabled"},
        }

    assert source_model.model_kwargs == {}
    assert source_model.reasoning_effort is None
    assert source_model.extra_body == {"user_id": "commitment-test"}


def test_stage_four_tool_worker_avoids_deepseek_json_auto_parse(
    monkeypatch,
):
    from langchain_openai import ChatOpenAI
    from openai.lib._parsing._completions import validate_input_tools
    from focus.agents.commitment import ReviewedDelegator
    import focus.agents.commitment.delegation as delegation_module

    source_model = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        extra_body={"user_id": "commitment-test"},
    )
    delegator = ReviewedDelegator(source_model, context7_tools=[])
    created_payloads = []

    def fake_create_agent(*, model, tools, **_kwargs):
        tool_bound_model = model.bind_tools(tools)
        payload = tool_bound_model.bound._get_request_payload([("human", "json")])
        payload.update(tool_bound_model.kwargs)
        if payload.get("response_format"):
            validate_input_tools(payload["tools"])
        created_payloads.append(payload)
        return object()

    async def fake_stream_agent(
        _agent,
        _messages,
        *,
        actor,
        stage,
        stream_id,
        attempt=1,
    ):
        del actor, stage, stream_id, attempt
        return {
            "messages": [
                AIMessage(
                    content=(
                        '{"result":{"files":[],"urls":[]},'
                        '"reasoning_summary":"没有引用材料"}'
                    )
                )
            ]
        }

    monkeypatch.setattr(delegation_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(delegator, "_stream_agent", fake_stream_agent)

    output = asyncio.run(
        delegator._worker(
            TaskEnvelope(
                stage=4,
                instruction="解析用户引用的文件与网址",
                context={"source_text": "没有引用材料"},
            ),
            "",
        )
    )

    assert output.result == {"files": [], "urls": []}
    assert len(created_payloads) == 1
    assert "response_format" not in created_payloads[0]
    assert created_payloads[0]["reasoning_effort"] == "high"
    assert created_payloads[0]["extra_body"] == {
        "user_id": "commitment-test",
        "thinking": {"type": "enabled"},
    }


def _delegator_with_script(worker_results, evaluator_results):
    from focus.agents.commitment import ReviewedDelegator

    model = _FakeModel(worker_results, evaluator_results)
    delegator = ReviewedDelegator(model, context7_tools=[])

    async def fake_worker(envelope, feedback="", supervisor_messages=None, attempt=1):
        index = min(model.worker_calls, len(model.worker_results) - 1)
        model.worker_calls += 1
        if index < len(model.worker_results) - 1 and feedback:
            # 反馈存在时返回下一项（重试场景由测试脚本驱动）
            pass
        return model.worker_results[index]

    async def fake_evaluator(envelope, worker_output, supervisor_messages=None, attempt=1,
                             structure_error="", reviewer_feedback=""):
        index = min(model.evaluator_calls, len(model.evaluator_results) - 1)
        model.evaluator_calls += 1
        return model.evaluator_results[index]

    delegator._worker = fake_worker
    delegator._evaluator = fake_evaluator
    return delegator, model


def test_review_loop_approves_on_first_attempt():
    from focus.agents.commitment.schemas import ReviewOutput

    delegator, model = _delegator_with_script(
        [WorkerOutput(result={"goal": "X"})],
        [ReviewOutput(approved=True, feedback="")],
    )
    envelope = TaskEnvelope(stage=1, instruction="明确目标")
    output, error = asyncio.run(delegator.run(envelope))
    assert output is not None and output.result == {"goal": "X"}
    assert error == ""


def test_review_loop_retries_until_three_attempts():
    from focus.agents.commitment.schemas import ReviewOutput

    delegator, model = _delegator_with_script(
        [WorkerOutput(result={"goal": "A"}), WorkerOutput(result={"goal": "B"}), WorkerOutput(result={"goal": "C"})],
        [ReviewOutput(approved=False, feedback="改"), ReviewOutput(approved=False, feedback="再改")],
    )
    # 第三次 Evaluator 通过
    model.evaluator_results.append(ReviewOutput(approved=True))
    envelope = TaskEnvelope(stage=1, instruction="明确目标")
    output, error = asyncio.run(delegator.run(envelope))
    assert output.result == {"goal": "C"}
    assert model.worker_calls == 3


def test_review_loop_returns_failure_after_three_rejections():
    from focus.agents.commitment.schemas import ReviewOutput

    delegator, model = _delegator_with_script(
        [WorkerOutput(result={"goal": "A"}), WorkerOutput(result={"goal": "B"}), WorkerOutput(result={"goal": "C"})],
        [ReviewOutput(approved=False, feedback="改")] * 3,
    )
    envelope = TaskEnvelope(stage=1, instruction="明确目标")
    output, error = asyncio.run(delegator.run(envelope))
    assert output is None
    assert error == "改"


# === 5. 阶段规则 ===

def test_validate_stage_two_rejects_overlap():
    error = _validate_stage_result(2, {
        "requirements": ["A", "B"],
        "discarded_requirements": ["B"],
        "compatibility_checks": [{"technology": "t", "application_type": "a", "ui_surface": "u",
                                  "runtime_platform": "p", "host_model": "m", "status": "verified"}],
        "conflicts": [],
    })
    assert "移出 requirements" in error


def test_validate_stage_two_rejects_version_only_unresolved_check():
    error = _validate_stage_result(2, {
        "requirements": ["使用 React 和 Next.js"],
        "discarded_requirements": [],
        "compatibility_checks": [
            {
                "technology": "React + Next.js",
                "application_type": "Web 应用",
                "ui_surface": "浏览器",
                "runtime_platform": "Node.js + 浏览器",
                "host_model": "Next.js 服务端",
                "status": "verified",
            },
            {
                "technology": "React/Next.js 的精确版本兼容组合（未提供版本号）",
                "application_type": "Web 应用技术栈",
                "ui_surface": "浏览器 + 构建期",
                "runtime_platform": "浏览器、Node.js",
                "host_model": "未指定（需在实现时锁定已知兼容版本）",
                "status": "unresolved",
            },
        ],
        "conflicts": [
            {
                "requirements": ["A", "B"],
                "conflict_type": "已处理冲突",
                "explanation": "人工已经裁决",
                "status": "resolved",
                "resolution": "保留 A，放弃 B",
            }
        ],
    })

    assert "compatibility_checks[1]" in error
    assert "精确版本" in error
    assert "阶段5" in error


def test_validate_stage_two_preserves_real_host_model_unresolved_check():
    assert _validate_stage_result(2, {
        "requirements": ["把未知 CMS 模块嵌入桌面宿主"],
        "discarded_requirements": [],
        "compatibility_checks": [{
            "technology": "未知 CMS 模块",
            "application_type": "CMS 模块",
            "ui_surface": "未知",
            "runtime_platform": "未知",
            "host_model": "桌面宿主是否支持嵌入尚无法核实",
            "status": "unresolved",
        }],
        "conflicts": [{
            "requirements": ["把未知 CMS 模块嵌入桌面宿主"],
            "conflict_type": "宿主模型未核实",
            "explanation": "缺少可验证的嵌入接口",
            "status": "open",
        }],
    }) is None


def test_stage_two_version_only_unresolved_retries_inside_review_loop():
    from focus.agents.commitment import ReviewedDelegator
    from focus.agents.commitment.schemas import ReviewOutput

    verified_check = {
        "technology": "React + Next.js",
        "application_type": "Web 应用",
        "ui_surface": "浏览器",
        "runtime_platform": "Node.js + 浏览器",
        "host_model": "Next.js 服务端",
        "status": "verified",
    }
    bad_output = WorkerOutput(result={
        "requirements": ["使用 React 和 Next.js"],
        "discarded_requirements": [],
        "compatibility_checks": [
            verified_check,
            {
                "technology": "React/Next.js 精确版本兼容组合（未提供版本号）",
                "application_type": "Web 应用技术栈",
                "ui_surface": "浏览器 + 构建期",
                "runtime_platform": "浏览器、Node.js",
                "host_model": "未指定（需在实现时锁定已知兼容版本）",
                "status": "unresolved",
            },
        ],
        "conflicts": [{
            "requirements": ["A", "B"],
            "conflict_type": "已处理冲突",
            "explanation": "人工已经裁决",
            "status": "resolved",
            "resolution": "保留 A，放弃 B",
        }],
    })
    good_output = WorkerOutput(result={
        "requirements": ["使用 React 和 Next.js"],
        "discarded_requirements": [],
        "compatibility_checks": [verified_check],
        "conflicts": [],
    })
    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])
    worker_feedback = []
    worker_outputs = iter((bad_output, good_output))

    async def fake_worker(
        envelope,
        feedback="",
        supervisor_messages=None,
        attempt=1,
    ):
        del envelope, supervisor_messages, attempt
        worker_feedback.append(feedback)
        return next(worker_outputs)

    async def fake_evaluator(
        envelope,
        worker_output,
        supervisor_messages=None,
        attempt=1,
        structure_error="",
        reviewer_feedback="",
    ):
        del envelope, worker_output, supervisor_messages, attempt, reviewer_feedback
        return ReviewOutput(
            approved=not structure_error,
            feedback=structure_error,
        )

    delegator._worker = fake_worker
    delegator._evaluator = fake_evaluator

    output, error = asyncio.run(
        delegator.run(TaskEnvelope(stage=2, instruction="要求与兼容性"))
    )

    assert output == good_output
    assert error == ""
    assert worker_feedback[0] == ""
    assert "阶段5" in worker_feedback[1]


def test_validate_stage_three_replacement_requires_verbatim_inheritance():
    messages = [
        ToolMessage(content=json.dumps({"status": "approved", "stage": 2, "result": {
            "requirements": ["A", "B"], "discarded_requirements": [], "compatibility_checks": [
                {"technology": "t", "application_type": "a", "ui_surface": "u",
                 "runtime_platform": "p", "host_model": "m", "status": "verified"}],
            "conflicts": [],
        }}), tool_call_id="c1", id="c1-result"),
    ]
    error = _validate_stage_result(3, {"requirements": [{"requirement": "X", "priority": 3}]}, messages)
    assert "逐字" in error


def test_validate_stage_three_accepts_verbatim():
    messages = [
        ToolMessage(content=json.dumps({"status": "approved", "stage": 2, "result": {
            "requirements": ["A"], "discarded_requirements": [], "compatibility_checks": [
                {"technology": "t", "application_type": "a", "ui_surface": "u",
                 "runtime_platform": "p", "host_model": "m", "status": "verified"}],
            "conflicts": [],
        }}), tool_call_id="c1", id="c1-result"),
    ]
    assert _validate_stage_result(3, {"requirements": [{"requirement": "A", "priority": 3}]}, messages) is None


def test_stage_three_normalization_does_not_invent_priorities():
    output = WorkerOutput(
        result={
            "requirements": [
                {"requirement": "A", "priority": "high"},
                {"requirement": "B"},
            ]
        },
        reasoning_summary="模型声称 A=3、B=2",
    )

    normalized = _normalize_stage_three_result(output, ["A", "B"])

    assert normalized == output
    error = _validate_stage_result(3, normalized.result)
    assert "requirements[0].priority" in error
    assert "实际值" in error
    assert "整数 1、2、3" in error


def test_stage_three_normalization_preserves_priorities_and_builds_summary():
    output = WorkerOutput(
        result={
            "requirements": [
                {"requirement": "A", "priority": 3},
                {"requirement": "B", "priority": 2},
            ]
        },
        reasoning_summary="错误的旧说明：A=2、B=3",
    )

    normalized = _normalize_stage_three_result(output, ["A", "B"])

    assert normalized.result == output.result
    assert "R1=3" in normalized.reasoning_summary
    assert "R2=2" in normalized.reasoning_summary
    assert "A=2" not in normalized.reasoning_summary
    assert "B=3" not in normalized.reasoning_summary
    assert "唯一事实来源" in normalized.reasoning_summary


@pytest.mark.parametrize(
    "assignments, expected_error",
    [
        ([{"requirement_id": "R1", "priority": 3}], "缺少"),
        (
            [
                {"requirement_id": "R1", "priority": 3},
                {"requirement_id": "R1", "priority": 2},
            ],
            "重复",
        ),
        (
            [
                {"requirement_id": "R1", "priority": 3},
                {"requirement_id": "R3", "priority": 2},
            ],
            "未知",
        ),
        (
            [
                {"requirement_id": "R1", "priority": 3},
                {"requirement_id": "R2", "priority": "high"},
            ],
            "整数 1、2、3",
        ),
    ],
)
def test_stage_three_priority_assignments_reject_invalid_ids(assignments, expected_error):
    output = WorkerOutput(result={"priority_assignments": assignments})

    normalized = _normalize_stage_three_result(output, ["A", "B"])
    error = _validate_stage_result(3, normalized.result, _stage_three_messages(["A", "B"]))

    assert error is not None
    assert expected_error in error


def test_stage_three_priority_assignments_join_stage_two_verbatim_text():
    requirements = [
        "R0: 交付一个可运行的团队任务管理 Web 应用 POC。",
        "  保留首尾空格与标点；不得改写。  ",
    ]
    output = WorkerOutput(
        result={
            "priority_assignments": [
                {"requirement_id": "R2", "priority": 1},
                {"requirement_id": "R1", "priority": 3},
            ]
        },
        reasoning_summary="模型自由说明不可信",
    )

    normalized = _normalize_stage_three_result(output, requirements)

    assert normalized.result == {
        "requirements": [
            {"requirement": requirements[0], "priority": 3},
            {"requirement": requirements[1], "priority": 1},
        ]
    }
    assert "R1=3" in normalized.reasoning_summary
    assert "R2=1" in normalized.reasoning_summary


def _stage_three_messages(requirements):
    return [
        ToolMessage(
            content=json.dumps(
                {
                    "status": "approved",
                    "stage": 2,
                    "result": {
                        "requirements": requirements,
                        "discarded_requirements": [],
                        "compatibility_checks": [
                            {
                                "technology": "t",
                                "application_type": "a",
                                "ui_surface": "u",
                                "runtime_platform": "p",
                                "host_model": "m",
                                "status": "verified",
                            }
                        ],
                        "conflicts": [],
                    },
                }
            ),
            tool_call_id="c2",
            id="c2-result",
        )
    ]


class _ScriptedStreamAgent:
    def __init__(self, runs):
        self.runs = list(runs)
        self.calls = 0

    async def astream(self, _input, stream_mode):
        run = self.runs[self.calls]
        self.calls += 1
        for item in run:
            yield item


def test_stream_agent_retries_reasoning_only_response_without_disabling_thinking(monkeypatch):
    from focus.agents.commitment import ReviewedDelegator
    import focus.agents.commitment.delegation as delegation_module

    published = []
    monkeypatch.setattr(
        delegation_module,
        "emit_commitment_messages",
        lambda **kwargs: published.extend(kwargs["messages"]),
    )
    empty = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "不得公开"},
        response_metadata={"finish_reason": "stop"},
    )
    final = AIMessage(
        content='{"result":{"goal":"X"}}',
        response_metadata={"finish_reason": "stop"},
    )
    agent = _ScriptedStreamAgent(
        [
            [("messages", (empty, {})), ("values", {"messages": [empty]})],
            [("messages", (final, {})), ("values", {"messages": [final]})],
        ]
    )
    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])

    result = asyncio.run(
        delegator._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
        )
    )

    assert agent.calls == 2
    assert result["messages"][-1].content == final.content
    assert all("不得公开" not in str(message.content) for message in published)


def test_stream_agent_reports_length_instead_of_json_eof():
    from focus.agents.commitment import ReviewedDelegator

    truncated = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "hidden"},
        response_metadata={"finish_reason": "length"},
    )
    agent = _ScriptedStreamAgent(
        [[("messages", (truncated, {})), ("values", {"messages": [truncated]})]]
    )
    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])

    with pytest.raises(RuntimeError, match="length|截断"):
        asyncio.run(
            delegator._stream_agent(
                agent,
                [HumanMessage(content="input")],
                actor="evaluator",
                stage=1,
                stream_id="evaluator-1",
            )
        )


def test_stream_agent_retries_insufficient_system_resource_with_partial_content():
    from focus.agents.commitment import ReviewedDelegator

    partial = AIMessage(
        content='{"result":',
        response_metadata={"finish_reason": "insufficient_system_resource"},
    )
    final = AIMessage(
        content='{"result":{"goal":"X"}}',
        response_metadata={"finish_reason": "stop"},
    )
    agent = _ScriptedStreamAgent(
        [
            [("messages", (partial, {})), ("values", {"messages": [partial]})],
            [("messages", (final, {})), ("values", {"messages": [final]})],
        ]
    )
    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])

    result = asyncio.run(
        delegator._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
        )
    )

    assert agent.calls == 2
    assert result["messages"][-1].content == final.content


def test_stream_agent_retries_incomplete_chunked_read():
    from httpx import RemoteProtocolError
    from focus.agents.commitment import ReviewedDelegator

    partial = AIMessage(content='{"result":')
    final = AIMessage(
        content='{"result":{"knowledge":[]}}',
        response_metadata={"finish_reason": "stop"},
    )

    class DisconnectingThenSuccessfulAgent:
        def __init__(self):
            self.calls = 0

        async def astream(self, _input, stream_mode):
            self.calls += 1
            if self.calls == 1:
                yield "messages", (partial, {})
                raise RemoteProtocolError(
                    "peer closed connection without sending complete message body "
                    "(incomplete chunked read)"
                )
            yield "messages", (final, {})
            yield "values", {"messages": [final]}

    agent = DisconnectingThenSuccessfulAgent()
    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])

    result = asyncio.run(
        delegator._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=6,
            stream_id="worker-6",
        )
    )

    assert agent.calls == 2
    assert result["messages"][-1].content == final.content


def test_stage_four_filters_non_referenced_urls():
    output = WorkerOutput(result={
        "files": [],
        "urls": [
            {"mention": "React docs", "url": "https://react.dev", "source": "user", "status": "provided"},
            {"mention": "TypeScript 官网", "url": "https://ts.org", "source": "user", "status": "provided"},
        ],
    })
    filtered = _filter_stage_four_result(output, "参考 React docs 完成，用 TypeScript 实现")
    urls = filtered.result["urls"]
    assert len(urls) == 1 and urls[0]["mention"] == "React docs"


def test_stage_four_needs_review_for_proposed():
    assert _stage_four_needs_review({"files": [{"status": "proposed", "mention": "a"}], "urls": []})
    assert not _stage_four_needs_review({"files": [{"status": "matched", "mention": "a"}], "urls": []})


def test_has_open_conflicts():
    assert _has_open_conflicts({"compatibility_checks": [{"status": "conflict"}], "conflicts": []})
    assert not _has_open_conflicts({"compatibility_checks": [{"status": "verified"}], "conflicts": []})


# === 6. Context7 解析 ===

def test_context7_library_id_parsing():
    result = "Available Libraries:\n- Title: LangGraph\n- Context7-compatible library ID: /langchain-ai/langgraph"
    assert _context7_library_id(result) == "/langchain-ai/langgraph"


def test_context7_stable_version_parsing():
    result = "The latest stable version of LangGraph is 1.0.8 (as of August 2026)"
    assert _context7_stable_version(result) == "1.0.8"


def test_context7_candidate_version_parsing():
    result = """- Title: LangGraph\n- Context7-compatible library ID: /langchain-ai/langgraph\n- Versions: 0.2.74, 0.4.8, 0.5.3, 0.6.0, 1.0.3"""
    assert _context7_candidate_version(result, "/langchain-ai/langgraph") == "1.0.3"


def test_safe_segment_rejects_unsafe_chars():
    assert _safe_segment("a-b_1.c", "x") == "a-b_1.c"
    with pytest.raises(ValueError):
        _safe_segment("../x", "x")


def test_stage_six_rejects_version_that_differs_from_stage_five():
    messages = [
        ToolMessage(
            content=json.dumps(
                {
                    "status": "approved",
                    "stage": 5,
                    "result": {
                        "technologies": [
                            {
                                "name": "Tailwind CSS",
                                "project_version": "unresolved",
                                "version": "latest-stable",
                                "version_basis": "latest_stable_policy",
                            }
                        ]
                    },
                }
            ),
            tool_call_id="c5",
        )
    ]
    result = {
        "knowledge": [
            {
                "technology": "Tailwind CSS",
                "version": "latest-stable (official body identifies v4.0)",
                "source_url": "https://tailwindcss.com/docs",
                "content": "Official documentation.",
            }
        ]
    }

    error = _validate_stage_result(6, result, messages)

    assert error is not None
    assert "latest-stable" in error


def test_stage_six_accepts_exact_stage_five_version():
    messages = [
        ToolMessage(
            content=json.dumps(
                {
                    "status": "approved",
                    "stage": 5,
                    "result": {
                        "technologies": [
                            {
                                "name": "Tailwind CSS",
                                "project_version": "unresolved",
                                "version": "latest-stable",
                                "version_basis": "latest_stable_policy",
                            }
                        ]
                    },
                }
            ),
            tool_call_id="c5",
        )
    ]
    result = {
        "knowledge": [
            {
                "technology": "Tailwind CSS",
                "version": "latest-stable",
                "source_url": "https://tailwindcss.com/docs",
                "content": "Official documentation.",
            }
        ]
    }

    assert _validate_stage_result(6, result, messages) is None


def test_stage_five_rejects_version_that_is_not_a_safe_path_segment():
    result = {
        "technologies": [
            {
                "name": "Tailwind CSS",
                "project_version": "unresolved",
                "version": "latest-stable (v4.0)",
                "version_basis": "latest_stable_policy",
            }
        ]
    }

    error = _validate_stage_result(5, result)

    assert error is not None
    assert "latest-stable" in error


# === 7. interrupt/resume 序列化 ===

def test_serialize_interrupt_roundtrip():
    interrupt = Interrupt(value={"type": "commitment_review", "stage": 3}, id="abc")
    serialized = serialize_interrupt(interrupt)
    assert serialized["id"] == "abc"
    assert serialized["value"]["stage"] == 3


def test_extract_interrupts_from_values_chunk():
    interrupt = Interrupt(value={"decision": "approve"}, id="abc")
    chunk = {"messages": [], "__interrupt__": (interrupt,)}
    interrupts = extract_interrupts(chunk)
    assert len(interrupts) == 1
    assert interrupts[0]["value"]["decision"] == "approve"


# === 8. Supervisor 消息账本 ===

def _stage_two_tool_message():
    return ToolMessage(
        content=json.dumps({"status": "approved", "stage": 2, "result": {"requirements": ["A"]}}),
        tool_call_id="c2", id="c2-result",
    )


def test_stage_result_reads_only_ledger():
    from focus.agents.commitment.stage_rules import _stage_result

    messages = [
        HumanMessage(content="/commit 做X", id="m1"),
        AIMessage(content="第 2 步", tool_calls=[{"name": "delegate_with_review", "args": {}, "id": "c2", "type": "tool_call"}], id="a2"),
        _stage_two_tool_message(),
    ]
    assert _stage_result(messages, 2) == {"requirements": ["A"]}
    assert _stage_result(messages, 5) == {}


def test_replace_stage_tool_message_in_place():
    from focus.agents.commitment.workflow import _replace_stage_tool_message

    messages = [_stage_two_tool_message(), _stage_two_tool_message()]
    replaced = _replace_stage_tool_message({"messages": messages}, 2, "新内容")
    assert replaced.content == "新内容"
    assert replaced.tool_call_id == "c2-result".replace("c2-result", "c2")


# === 9. Supervisor 子图路由 ===

def test_supervisor_routes_to_human_review_when_awaiting():
    from focus.agents.commitment.workflow import _route_supervisor

    assert _route_supervisor({"stage": 3, "awaiting_human": 3, "messages": []}) == "human_review"
    assert _route_supervisor({"stage": 9, "awaiting_human": None, "messages": []}) == "__end__"
    assert _route_supervisor({"stage": 2, "awaiting_human": None, "messages": [_stage_two_tool_message()]}) == "prepare_call"


def test_supervisor_builds_with_mock_delegator():
    from focus.agents.commitment import ReviewedDelegator

    delegator = ReviewedDelegator(_FakeModel([], []), context7_tools=[])
    graph = _build_supervisor(delegator)
    assert graph is not None


# === 10. 最终消息与合同落盘 ===

def test_write_contract_and_knowledge_to_workspace(tmp_path):
    from focus.agents.commitment.artifacts import _write_contract, _write_knowledge

    knowledge_files = _write_knowledge({"knowledge": [
        {"technology": "LangGraph", "version": "1.0.8", "source_url": "https://docs.example.com", "content": "知识正文"},
    ]}, str(tmp_path))
    assert knowledge_files == ["knowledge/LangGraph-1.0.8.md"]
    assert (tmp_path / "knowledge" / "LangGraph-1.0.8.md").exists()

    contract, rel = _write_contract("th-1", {"contract_markdown": "# 合同"}, str(tmp_path))
    assert contract == "# 合同"
    assert rel == "requirements/th-1/task-contract.md"
    assert (tmp_path / "requirements" / "th-1" / "task-contract.md").read_text(encoding="utf-8") == "# 合同\n"


def test_final_message_bundles_contract_and_foundation(tmp_path):
    from focus.agents.commitment.artifacts import _build_final_message, _write_knowledge

    knowledge = _write_knowledge({"knowledge": [
        {"technology": "LangGraph", "version": "1.0.8", "source_url": "https://x", "content": "正文"},
    ]}, str(tmp_path))
    final = _build_final_message("合同内容", knowledge, str(tmp_path))
    assert "<task_contract>" in final and "合同内容" in final
    assert "theoretical foundation" in final and "正文" in final


# === 11. 端到端：子图驱动 + 人工批准循环 + 合同落盘 ===

class _ScriptedDelegator:
    """按阶段返回合法结果的脚本委派器（不调模型）。"""

    def __init__(self):
        self.calls: list[int] = []

    async def run(self, envelope, supervisor_messages=None):
        self.calls.append(envelope.stage)
        results = {
            1: {"goal": "完成 X"},
            2: {
                "requirements": ["使用 LangGraph 完成"],
                "discarded_requirements": [],
                "compatibility_checks": [{
                    "technology": "LangGraph", "application_type": "desktop",
                    "ui_surface": "local", "runtime_platform": "windows",
                    "host_model": "deepseek-v4-flash", "status": "verified",
                }],
                "conflicts": [],
            },
            3: {"requirements": [{"requirement": "使用 LangGraph 完成", "priority": 3}]},
            4: {"files": [], "urls": []},
            5: {"technologies": [{
                "name": "LangGraph", "project_version": "unresolved",
                "version": "latest-stable", "library_id": "/langchain-ai/langgraph",
                "source_url": None, "version_basis": "latest_stable_policy",
                "version_evidence": "Context7 未返回可核实的精确稳定版本。",
            }]},
            6: {"knowledge": [{
                "technology": "LangGraph", "version": "latest-stable",
                "source_url": "https://docs.langchain.com", "content": "LangGraph 官方知识正文。",
            }]},
            7: {"contract_markdown": "# 任务合同\n\n目标：完成 X。"},
        }
        return WorkerOutput(result=results[envelope.stage]), ""

    async def _evaluator(self, envelope, worker_output, supervisor_messages=None,
                         attempt=1, structure_error="", reviewer_feedback=""):
        from focus.agents.commitment.schemas import ReviewOutput

        return ReviewOutput(approved=True, feedback="")


def test_end_to_end_subgraph_with_approval_cycle(tmp_path):
    from langgraph._internal._constants import CONFIG_KEY_CHECKPOINTER
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from focus.agents.commitment.workflow import _build_supervisor

    delegator = _ScriptedDelegator()
    supervisor = _build_supervisor(delegator)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "th-1:commitment", CONFIG_KEY_CHECKPOINTER: saver}}

    trigger = HumanMessage(content="/commit 做X", id="m1")
    seed = {
        "messages": [trigger.model_copy(update={"content": "做X"})],
        "stage": 0, "awaiting_human": None, "artifacts": {},
        "thread_id": "th-1", "workspace": str(tmp_path),
        "knowledge_files": [], "source_text": "做X",
        "uploads_tag": "<current_uploads>\n材料.md\n</current_uploads>",
    }

    async def drive(graph_input):
        result = {}
        async for _mode, chunk in supervisor.astream(
            graph_input, config=config, stream_mode=["values", "custom"]
        ):
            if _mode == "values":
                result = chunk
        return result

    async def run_flow():
        result = await drive(seed)
        # 首次执行：阶段 1-3 推进后固定中断于阶段 3 人工确认
        assert result.get("stage") == 3
        interrupts = result.get("__interrupt__")
        assert interrupts, "子图必须在人工确认处中断"
        # 依次批准阶段 3/5/6/7 及阶段 2/4 条件确认（本脚本无冲突/无候选，仅 3/5/6/7）
        approve_count = 0
        while result.get("stage") != 9:
            interrupts = result.get("__interrupt__")
            if not interrupts:
                break
            interrupt = interrupts[0]
            value = interrupt.value
            assert value["type"] == "commitment_review"
            assert "approve" in value["allowed_decisions"]
            result = await drive(Command(resume={"decision": "approve"}))
            approve_count += 1
        return result, approve_count

    result, approve_count = asyncio.run(run_flow())
    assert result.get("stage") == 9, result.get("stage")
    assert approve_count == 4  # 阶段 3/5/6/7
    assert result.get("task_contract") == "# 任务合同\n\n目标：完成 X。"
    assert (tmp_path / "requirements" / "th-1" / "task-contract.md").exists()
    assert (tmp_path / "knowledge" / "LangGraph-latest-stable.md").exists()
    final_message = result.get("final_message", "")
    assert "<task_contract>" in final_message and "theoretical foundation" in final_message
    # 阶段委派循环：每阶段恰好一组 AIMessage–ToolMessage
    ledger = result.get("messages", [])
    from langchain.messages import AIMessage as _AI, ToolMessage as _Tool

    stage_messages = [m for m in ledger if isinstance(m, (_AI, _Tool))]
    assert len(stage_messages) == 18  # 9 阶段 × (1 AIMessage + 1 ToolMessage)
    # 账本起点为指令 HumanMessage（沿用触发消息 id）
    assert isinstance(ledger[0], HumanMessage) and ledger[0].id == "m1"
    assert len(delegator.calls) == 7  # 阶段 1-7 走 Worker，8/9 为确定性节点


# === 12. 结构化 JSON 提取边界 ===

def test_extract_structured_with_surrounding_text():
    from focus.agents.commitment.stage_rules import _extract_structured
    from focus.agents.commitment.schemas import WorkerOutput
    from langchain.messages import AIMessage

    result = {
        "messages": [AIMessage(
            content='好的，以下是分析结果：\n```json\n{"result": {"goal": "X"}, "reasoning_summary": "r"}\n```\n以上仅供参考。',
            id="a1",
        )]
    }
    assert _extract_structured(result, WorkerOutput).result == {"goal": "X"}


def test_extract_structured_plain_text_around_json():
    from focus.agents.commitment.stage_rules import _extract_structured
    from focus.agents.commitment.schemas import WorkerOutput
    from langchain.messages import AIMessage

    result = {
        "messages": [AIMessage(
            content='根据分析，结论如下 {"result": {"goal": "Y"}, "reasoning_summary": "r2"} 完毕。',
            id="a1",
        )]
    }
    with pytest.raises(ValueError):
        _extract_structured(result, WorkerOutput)


def test_extract_structured_pure_json_still_works():
    from focus.agents.commitment.stage_rules import _extract_structured
    from focus.agents.commitment.schemas import WorkerOutput
    from langchain.messages import AIMessage

    result = {
        "messages": [AIMessage(
            content='{"result": {"goal": "Z"}, "reasoning_summary": "r3"}',
            id="a1",
        )]
    }
    assert _extract_structured(result, WorkerOutput).result == {"goal": "Z"}
