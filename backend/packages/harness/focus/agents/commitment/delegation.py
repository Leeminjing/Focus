"""
本文件对外提供 ReviewedDelegator，封装隔离的 Worker-Evaluator 审核闭环。

输入:
    model — 创建 Worker 和 Evaluator 所使用的 BaseChatModel。
    context7_tools — 仅承诺层内部可见的 Context7 BaseTool 列表。
    TaskEnvelope — 当前阶段指令、上下文和验收条件。
    Supervisor messages — 指令 HumanMessage 和此前阶段最终 ToolMessage。

输出:
    WorkerOutput | None — 最多三次审核后通过的阶段结果；全部失败时为 None。
    str — 未通过时最后一轮结构校验或 Evaluator 反馈。

具体工作流:
    (1) 为当前阶段创建干净的 Worker 上下文并生成候选结果。
    (2) 对特殊阶段执行确定性 Context7 查询、版本处理或结果规范化。
    (3) 使用独立 Evaluator 按 TaskEnvelope 验收条件审核结果。
    (4) Evaluator 不合格时携带反馈创建新的 Worker，最多执行三次。
    (5) 流式发布 Worker 和 Evaluator 各自的真实 messages，不读取私密 reasoning 字段。
    (6) 工具只向 Supervisor 返回最终通过结果或最后反馈。

示例:
    delegator = ReviewedDelegator(model, context7_tools)
    output, feedback = await delegator.run(envelope)
"""

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage
from langchain_core.messages import BaseMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from httpx import TransportError
from pydantic import BaseModel, ValidationError

from focus.agents.commitment.references import find_reference_urls
from focus.agents.commitment.schemas import (
    ReviewOutput,
    StageFourResult,
    StageTwoResult,
    TaskEnvelope,
    TechnologySelection,
    WorkerOutput,
)
from focus.agents.commitment.stage_rules import (
    _LATEST_STABLE_VERSION,
    _MAX_REVIEW_ATTEMPTS,
    _context7_candidate_version,
    _context7_library_id,
    _context7_stable_version,
    _context7_version_evidence,
    _extract_structured,
    _filter_stage_four_result,
    _json,
    _normalize_stage_three_result,
    _source_text,
    _stage_result,
    _stage_three_requirements,
    _validate_stage_result,
)
from focus.agents.commitment.tracing import (
    emit_commitment_messages,
    emit_commitment_trace,
)


class _ModelOutputError(RuntimeError):
    """模型请求完成但没有可解析的最终公开正文。"""


def _public_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(
            item
            if isinstance(item, str)
            else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    return content if isinstance(content, str) else ""


def _last_ai_message(result: dict[str, Any]) -> AIMessage | None:
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage):
            return message
    return None


def _finish_reason(message: AIMessage | None) -> str:
    if message is None:
        return ""
    metadata = getattr(message, "response_metadata", {})
    if not isinstance(metadata, dict):
        return ""
    reason = metadata.get("finish_reason")
    return str(reason) if reason else ""


def _deepseek_commitment_model(
    model: BaseChatModel,
    *,
    json_output: bool,
) -> BaseChatModel:
    """复制 DeepSeek V4 配置，并按调用是否带工具选择 JSON Output。"""
    if not isinstance(model, ChatOpenAI):
        return model
    model_name = str(getattr(model, "model_name", "") or "")
    if not model_name.startswith("deepseek-v4-"):
        return model

    model_kwargs = dict(model.model_kwargs or {})
    if json_output:
        model_kwargs["response_format"] = {"type": "json_object"}
    else:
        model_kwargs.pop("response_format", None)

    extra_body = dict(model.extra_body or {})
    thinking = extra_body.get("thinking")
    thinking_config = dict(thinking) if isinstance(thinking, dict) else {}
    thinking_config["type"] = "enabled"
    extra_body["thinking"] = thinking_config

    return model.model_copy(
        update={
            "model_kwargs": model_kwargs,
            "reasoning_effort": "high",
            "extra_body": extra_body,
        }
    )


def _deepseek_structured_model(model: BaseChatModel) -> BaseChatModel:
    """为不绑定工具的承诺层结构化请求启用稳定 JSON Output。"""
    return _deepseek_commitment_model(model, json_output=True)


def _deepseek_tool_model(model: BaseChatModel) -> BaseChatModel:
    """为工具型 Worker 保留 thinking/high，但避开 beta auto-parse。"""
    return _deepseek_commitment_model(model, json_output=False)


class ReviewedDelegator:
    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        self._model = model
        self._structured_model = _deepseek_structured_model(model)
        self._tool_model = _deepseek_tool_model(model)
        self._context7_tools = context7_tools

    async def _stream_agent(
        self,
        agent: Any,
        messages: list[HumanMessage],
        *,
        actor: str,
        stage: int,
        stream_id: str,
        attempt: int = 1,
    ) -> dict[str, Any]:
        emit_commitment_messages(
            actor=actor,
            stage=stage,
            attempt=attempt,
            messages=messages,
        )
        last_reason = "missing_final_message"
        for transport_attempt in range(1, 3):
            result: dict[str, Any] = {}
            try:
                async for mode, chunk in agent.astream(
                    {"messages": messages},
                    stream_mode=["messages", "values"],
                ):
                    if mode == "values":
                        result = chunk
                        continue
                    if mode != "messages":
                        continue
                    message_chunk, _metadata = chunk
                    content = _public_content(message_chunk)
                    if not content:
                        continue
                    emit_commitment_messages(
                        actor=actor,
                        stage=stage,
                        attempt=attempt,
                        messages=[message_chunk],
                    )
            except TransportError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                if transport_attempt < 2:
                    emit_commitment_trace(
                        actor=actor,
                        event="model_transport_retry",
                        title=f"{actor} 模型流连接中断，正在重试",
                        status="running",
                        stage=stage,
                        detail=(
                            f"transport_attempt={transport_attempt}; "
                            f"error={last_reason}"
                        ),
                    )
                continue
            final_message = _last_ai_message(result)
            finish_reason = _finish_reason(final_message)
            if finish_reason == "length":
                raise _ModelOutputError("模型输出被截断（finish_reason=length）")
            if (
                finish_reason != "insufficient_system_resource"
                and final_message is not None
                and _public_content(final_message)
            ):
                return result
            last_reason = finish_reason or "missing_final_message"
            if transport_attempt < 2:
                emit_commitment_trace(
                    actor=actor,
                    event="model_transport_retry",
                    title=f"{actor} 模型正文为空，正在重试",
                    status="running",
                    stage=stage,
                    detail=(
                        f"transport_attempt={transport_attempt}; "
                        f"finish_reason={last_reason}"
                    ),
                )
        raise _ModelOutputError(
            "模型传输重试耗尽，未得到最终公开正文；"
            f"finish_reason={last_reason}"
        )

    async def _invoke_schema(
        self,
        *,
        name: str,
        system_prompt: str,
        prompt: dict[str, Any],
        schema: type[BaseModel],
        attempt: int,
    ) -> BaseModel:
        emit_commitment_trace(
            actor="worker",
            event="model_started",
            title=f"正在运行 {name}",
            status="running",
            stage=int(prompt.get("stage", 0) or 0),
            payload={"input": prompt},
        )
        agent = create_agent(
            model=self._structured_model,
            tools=[],
            system_prompt=system_prompt,
            name=name,
        )
        result = await self._stream_agent(
            agent,
            [HumanMessage(content=_json(prompt))],
            actor="worker",
            stage=int(prompt.get("stage", 0) or 0),
            stream_id=name,
            attempt=attempt,
        )
        output = _extract_structured(result, schema)
        emit_commitment_trace(
            actor="worker",
            event="model_completed",
            title=f"{name} 已返回",
            status="completed",
            stage=int(prompt.get("stage", 0) or 0),
            payload={"output": output.model_dump()},
        )
        return output

    def _context7_tool(self, name: str) -> BaseTool:
        for tool_item in self._context7_tools:
            if tool_item.name == name:
                return tool_item
        raise RuntimeError(f"Context7 工具不可用: {name}")

    async def _stage_five_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        stage_three = _stage_result(supervisor_messages, 3)
        selection = await self._invoke_schema(
            name="commitment_technology_selector",
            system_prompt=(
                "从第三步已确认要求中提取需要核验版本的具体软件技术、框架、库和服务名称。"
                "不要返回SSR、SEO、错误处理等没有独立软件版本的能力或质量要求。"
                "只返回 JSON。"
                f"JSON Schema: {_json(TechnologySelection.model_json_schema())}"
            ),
            prompt={
                "stage": envelope.stage,
                "supervisor_messages": [
                    message.model_dump() for message in supervisor_messages
                ],
                "requirements": stage_three.get("requirements", []),
                "reviewer_feedback": feedback,
            },
            schema=TechnologySelection,
            attempt=attempt,
        )
        names = list(
            dict.fromkeys(
                name.strip()
                for name in selection.technologies
                if name.strip()
            )
        )
        resolver = self._context7_tool("resolve-library-id")

        async def resolve(name: str) -> dict[str, Any]:
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 resolve-library-id",
                status="running",
                stage=envelope.stage,
                detail=f"正在解析 {name} 的 Context7 库标识。",
                payload={"libraryName": name},
            )
            try:
                result = await resolver.ainvoke(
                    {
                        "libraryName": name,
                        "query": (
                            f"{name} latest stable version and official documentation"
                        ),
                    }
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 resolve-library-id 已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的库标识解析完成。",
                    payload={
                        "name": name,
                        "library_id": _context7_library_id(result),
                    },
                )
                return {"name": name, "result": result}
            except Exception as exc:
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 resolve-library-id 调用失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                )
                return {"name": name, "error": str(exc)}

        resolution_evidence = await asyncio.gather(
            *(resolve(name) for name in names)
        )
        query_docs = self._context7_tool("query-docs")

        async def query_version(item: dict[str, Any]) -> dict[str, Any]:
            name = str(item.get("name", ""))
            library_id = _context7_library_id(item.get("result"))
            if not library_id:
                return {"name": name, "error": "library_id unresolved"}
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 query-docs",
                status="running",
                stage=envelope.stage,
                detail=f"正在核实 {name} 的最新稳定版本。",
                payload={"libraryId": library_id},
            )
            try:
                result = await query_docs.ainvoke(
                    {
                        "libraryId": library_id,
                        "query": (
                            f"What is the exact latest stable {name} version? "
                            "Use official release or installation documentation "
                            "and exclude canary, beta, rc, and nightly versions."
                        ),
                    }
                )
                official_version = _context7_stable_version(result)
                candidate_version = _context7_candidate_version(
                    item.get("result"),
                    library_id,
                )
                version_hint = official_version or candidate_version
                evidence_source = (
                    result if official_version else item.get("result")
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 query-docs 已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的版本证据已完成处理。",
                    payload={
                        "name": name,
                        "library_id": library_id,
                        "version": version_hint or _LATEST_STABLE_VERSION,
                    },
                )
                return {
                    "name": name,
                    "library_id": library_id,
                    "version_hint": version_hint,
                    "version_basis": (
                        "official_docs_explicit"
                        if official_version
                        else (
                            "context7_version_list"
                            if candidate_version
                            else "unresolved"
                        )
                    ),
                    "version_evidence": (
                        _context7_version_evidence(
                            evidence_source,
                            version_hint,
                        )
                        if version_hint
                        else None
                    ),
                    "result": result,
                }
            except Exception as exc:
                candidate_version = _context7_candidate_version(
                    item.get("result"),
                    library_id,
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 query-docs 调用失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                    payload={
                        "fallback_version": candidate_version
                        or _LATEST_STABLE_VERSION
                    },
                )
                return {
                    "name": name,
                    "library_id": library_id,
                    "version_hint": candidate_version,
                    "version_basis": (
                        "context7_version_list"
                        if candidate_version
                        else "unresolved"
                    ),
                    "version_evidence": (
                        _context7_version_evidence(
                            item.get("result"),
                            candidate_version,
                        )
                        if candidate_version
                        else None
                    ),
                    "error": str(exc),
                }

        version_evidence = await asyncio.gather(
            *(query_version(item) for item in resolution_evidence)
        )
        by_name = {
            str(item.get("name")): item
            for item in version_evidence
        }
        return WorkerOutput(
            result={
                "technologies": [
                    {
                        "name": name,
                        "project_version": "unresolved",
                        "version": (
                            by_name.get(name, {}).get("version_hint")
                            or _LATEST_STABLE_VERSION
                        ),
                        "library_id": by_name.get(name, {}).get("library_id"),
                        "source_url": None,
                        "version_basis": by_name.get(name, {}).get(
                            "version_basis",
                        )
                        if by_name.get(name, {}).get("version_hint")
                        else "latest_stable_policy",
                        "version_evidence": (
                            by_name.get(name, {}).get("version_evidence")
                            if by_name.get(name, {}).get("version_hint")
                            else (
                                "Context7未返回可核实的精确稳定版本；"
                                "按用户策略在执行时采用最新稳定版。"
                            )
                        ),
                    }
                    for name in names
                ]
            },
            reasoning_summary=(
                "我先从第三步已确认要求中提取需要独立版本的技术，再解析各自的 "
                "Context7 library_id，并优先采用官方文档明确版本，其次采用过滤预发布"
                "版本后的 Context7 版本列表。没有可核实精确版本的技术按已确认策略使用 "
                "latest-stable；本阶段没有数值计算。"
            ),
        )

    async def _stage_six_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        technologies = _stage_result(supervisor_messages, 5).get(
            "technologies",
            [],
        )
        query_docs = self._context7_tool("query-docs")

        async def query(item: dict[str, Any]) -> dict[str, Any]:
            name = str(item.get("name", ""))
            library_id = str(item.get("library_id", ""))
            if not library_id or library_id == "unresolved":
                return {"name": name, "error": "library_id unresolved"}
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 query-docs",
                status="running",
                stage=envelope.stage,
                detail=f"正在读取 {name} {item.get('version', '')} 的官方实现知识。",
                payload={"libraryId": library_id},
            )
            try:
                result = await query_docs.ainvoke(
                    {
                        "libraryId": library_id,
                        "query": (
                            f"Official implementation knowledge required for "
                            f"{name} {item.get('version', '')}"
                        ),
                    }
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 官方知识已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的官方知识已交给 Worker 组装。",
                )
                return {"name": name, "version": item.get("version"), "result": result}
            except Exception as exc:
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 官方知识查询失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                )
                return {"name": name, "error": str(exc)}

        evidence = await asyncio.gather(
            *(query(item) for item in technologies if isinstance(item, dict))
        )
        return await self._invoke_schema(
            name="commitment_knowledge_assembler",
            system_prompt=(
                "仅根据给定 Context7 官方文档证据组装第六步 knowledge。"
                "每项包含technology、version、source_url、content；没有官方来源或正文时"
                "不得猜测。最终返回 WorkerOutput JSON，并用reasoning_summary自然说明"
                "结论依据、关键步骤、必要计算、方案比较以及不确定点与假设。"
                f"WorkerOutput Schema: {_json(WorkerOutput.model_json_schema())}"
            ),
            prompt={
                "stage": envelope.stage,
                "supervisor_messages": [
                    message.model_dump() for message in supervisor_messages
                ],
                "confirmed_technologies": technologies,
                "context7_evidence": evidence,
                "reviewer_feedback": feedback,
            },
            schema=WorkerOutput,
            attempt=attempt,
        )

    async def _worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        if envelope.stage == 5:
            return await self._stage_five_worker(
                envelope,
                feedback,
                supervisor_messages,
                attempt,
            )
        if envelope.stage == 6:
            return await self._stage_six_worker(
                envelope,
                feedback,
                supervisor_messages,
                attempt,
            )
        async def traced_find_reference_urls(query: str) -> list[dict[str, str]]:
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="搜索用户提到的网址",
                status="running",
                stage=envelope.stage,
                detail=f"正在搜索：{query}",
            )
            result = await find_reference_urls.ainvoke({"query": query})
            emit_commitment_trace(
                actor="tool",
                event="tool_completed",
                title="网址候选搜索已返回",
                status="completed",
                stage=envelope.stage,
                detail=f"找到 {len(result)} 个候选结果。",
                payload={"query": query, "results": result},
            )
            return result

        tools = (
            [
                StructuredTool.from_function(
                    coroutine=traced_find_reference_urls,
                    name="find_reference_urls",
                    description="Search for candidate URLs explicitly mentioned by the user.",
                )
            ]
            if envelope.stage == 4
            else []
        )
        if envelope.stage == 2:
            stage_guidance = (
                "第二步必须严格按以下 JSON Schema 生成 result："
                f"{_json(StageTwoResult.model_json_schema())}。"
                "requirements只包含冲突解决后仍需完成的要求；用户明确放弃的要求必须从"
                "requirements移入discarded_requirements，二者不得重叠。"
                "每个冲突必须是对象；未由人解决时status=open，不能省略或写成字符串。"
                "本步骤只核验应用类型、UI载体、运行平台和宿主模型，不识别或锁定精确版本。"
                "不得为‘未提供版本号’、‘待实现时锁定版本’或精确版本组合额外生成"
                "unresolved compatibility_check；版本识别、latest-stable和版本人工确认属于第五步。"
            )
        elif envelope.stage == 3:
            stage_guidance = (
                "第三步不得重复生成、改写或省略requirement正文。"
                "只读取输入priority_requirements中的稳定requirement_id与对应原文，"
                "必须排除discarded_requirements，已放弃的要求不得分配priority。"
                "只在本步骤首次分配priority；第二步不包含优先级，"
                "不得声称或推断第二步已为任何要求分配等级。"
                "result只能包含priority_assignments列表；每项只能包含requirement_id和"
                "整数priority 1、2或3，每个已知ID恰好出现一次。"
                "受控代码不会按数组位置、关键词或默认值猜测。"
                "示例：result={\"priority_assignments\":[{\"requirement_id\":\"R1\",\"priority\":3}]}。"
                "最终result.requirements和公开reasoning_summary将由受控代码根据阶段2原文和"
                "显式priority确定性生成。"
            )
        elif envelope.stage == 4:
            stage_guidance = (
                "第四步必须严格按以下 JSON Schema 生成 result："
                f"{_json(StageFourResult.model_json_schema())}。"
                "只处理用户引用的文件和网址。用<current_uploads>核对文件名；"
                "网站或文档名称没有URL时必须调用find_reference_urls。"
                "仅“参考、依据、按照文档/网站/链接”等明确引用意图算引用；"
                "“使用Next.js”等技术要求不是网址引用，没有引用时files和urls都返回空列表。"
                "候选只能标记proposed，不能替用户确认。"
            )
        elif envelope.stage == 5:
            stage_guidance = (
                "第五步必须从Supervisor messages中阶段3 ToolMessage的requirements识别全部涉及技术，"
                "逐项调用Context7核验。result.technologies不得为空；"
                "每项必须包含name、project_version、version、source_url，"
                "无法确认精确版本时version写latest-stable并使用latest_stable_policy，"
                "不得省略技术或猜测版本号。"
            )
        elif envelope.stage == 7:
            stage_guidance = (
                "第七步必须返回result.contract_markdown，值为完整Markdown任务合同；"
                "不得使用contract、markdown或content等替代字段名。"
            )
        else:
            stage_guidance = ""
        agent = create_agent(
            model=self._tool_model if tools else self._structured_model,
            tools=tools,
            system_prompt=(
                "你是承诺层 Worker。只处理当前阶段，使用工具核实版本和官方资料；"
                "不得依赖未核实的世界知识。最终只返回一个 JSON 对象，不要 Markdown。"
                "reasoning_summary用一段自然语言简洁说明结论依据、关键步骤、必要计算、"
                "方案比较以及不确定点与假设；不要输出隐藏思维链。"
                "兼容性判断必须区分应用类型、UI载体、运行平台与宿主模型；"
                "同属一种编程语言或运行时不代表可以组合。官方资料不足时标记unresolved。"
                f"{stage_guidance}"
                f"JSON Schema: {_json(WorkerOutput.model_json_schema())}"
            ),
            name=f"commitment_worker_{envelope.stage}",
        )
        prompt = {
            "stage": envelope.stage,
            "instruction": envelope.instruction,
            "source_text": envelope.context.get("source_text", ""),
            "supervisor_messages": [
                message.model_dump() for message in supervisor_messages
            ],
            "human_feedback": envelope.context.get("human_feedback", ""),
            "current_uploads": envelope.context.get("current_uploads", ""),
            "acceptance_criteria": envelope.acceptance_criteria,
            "reviewer_feedback": feedback,
        }
        if envelope.stage == 3:
            prompt["priority_requirements"] = [
                {
                    "requirement_id": f"R{index}",
                    "requirement": requirement,
                }
                for index, requirement in enumerate(
                    _stage_three_requirements(supervisor_messages),
                    start=1,
                )
            ]
        result = await self._stream_agent(
            agent,
            [HumanMessage(content=_json(prompt))],
            actor="worker",
            stage=envelope.stage,
            stream_id=f"worker-{envelope.stage}",
            attempt=attempt,
        )
        output = _extract_structured(result, WorkerOutput)
        if envelope.stage == 4:
            return _filter_stage_four_result(
                output,
                envelope.context.get("source_text", "")
                or _source_text(supervisor_messages),
            )
        if envelope.stage == 3:
            return _normalize_stage_three_result(
                output,
                _stage_three_requirements(supervisor_messages),
            )
        return output

    async def _evaluator(
        self,
        envelope: TaskEnvelope,
        worker_output: WorkerOutput,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
        structure_error: str = "",
        reviewer_feedback: str = "",
    ) -> ReviewOutput:
        supervisor_messages = supervisor_messages or []
        evaluator_input = {
            "supervisor_messages": [
                message.model_dump() for message in supervisor_messages
            ],
            "task": envelope.model_dump(),
            "source_text": envelope.context.get("source_text", ""),
            "current_uploads": envelope.context.get("current_uploads", ""),
            "reviewer_feedback": reviewer_feedback,
            "worker_output": worker_output.model_dump(),
            "deterministic_validation_error": structure_error,
        }
        if structure_error:
            review = ReviewOutput(
                approved=False,
                feedback=structure_error,
                reasoning_summary="候选结果未通过当前阶段的确定性结构校验，需要重试。",
            )
            emit_commitment_messages(
                actor="evaluator",
                stage=envelope.stage,
                attempt=attempt,
                messages=[
                    HumanMessage(content=_json(evaluator_input)),
                    AIMessage(content=_json(review.model_dump())),
                ],
            )
            return review
        agent = create_agent(
            model=self._structured_model,
            tools=[],
            system_prompt=(
                "你是独立 Evaluator。严格按验收条件判断 Worker 结果。"
                "reasoning_summary用一段自然语言说明审核依据、关键检查、必要计算、"
                "替代判断以及不确定点与假设；不要输出隐藏思维链。"
                "存在遗漏、臆测版本、非官方来源或结构错误时必须拒绝并给出可执行反馈。"
                "拒绝时feedback必须自包含：明确指出问题字段或索引、实际值、违反的规则和"
                "下一轮应执行的具体修订；不得只写‘请修正’或依赖下一轮不可见的失败候选。"
                "特别检查框架与组件的应用类型、UI载体、运行平台和宿主模型；"
                "Web、桌面、移动、CMS模块与独立应用不可仅因同属一种语言或运行时而判为兼容。"
                "仅在第五步，无法核实精确版本的技术才使用latest-stable策略并进入人工确认。"
                "第二步只核验应用类型、UI载体、运行平台和宿主模型；不得因未提供精确版本"
                "生成unresolved compatibility_check或要求第二步人工确认。"
                "第三步只检查要求是否逐字、逐项沿用第二步结果以及priority是否为1到3；"
                "第二步discarded_requirements中的已放弃要求不得出现在第三步；"
                "第二步不包含优先级，严禁声称或推断第二步已为任何要求分配等级。"
                "第四步仅核验明确引用的文件和网址；采用某项技术不等于引用其网站，"
                "没有明确引用时files和urls为空是合格结果。"
                "第五步technologies不得为空，且必须覆盖第三步要求涉及的全部技术；"
                "每项必须有name和version，未核实精确版本时使用latest-stable。第五步的具体 "
                "version由受控代码直接取自Context7官方文档的明确稳定版，或Context7所选库的"
                "版本列表在过滤canary、beta、rc、nightly后得到；原始工具回包不会进入"
                "WorkerOutput，但version_basis和version_evidence是受控代码截取的审核证据，"
                "是可信输入而非Worker的主张。version_basis为official_docs_explicit且有证据，"
                "或为context7_version_list且有证据时，必须视为已核实；过滤已经由受控代码完成。"
                "你不得用自身世界知识、记忆中的版本历史或缺少完整原始回包为由推翻这些结论。"
                "第五步只审核技术覆盖、字段结构以及具体版本是否同时具有basis和evidence；"
                "latest-stable配合latest_stable_policy是合格结果，随后由人工节点确认。"
                "最终只返回一个 JSON 对象，不要 Markdown。"
                f"JSON Schema: {_json(ReviewOutput.model_json_schema())}"
            ),
            name=f"commitment_evaluator_{envelope.stage}",
        )
        try:
            result = await self._stream_agent(
                agent,
                [HumanMessage(content=_json(evaluator_input))],
                actor="evaluator",
                stage=envelope.stage,
                stream_id=f"evaluator-{envelope.stage}",
                attempt=attempt,
            )
            return _extract_structured(result, ReviewOutput)
        except (ValidationError, _ModelOutputError) as exc:
            review = ReviewOutput(
                approved=False,
                feedback=f"Evaluator输出不符合ReviewOutput结构：{exc}",
                reasoning_summary="Evaluator输出结构无效，需要重新执行Worker-Evaluator审核。",
            )
            emit_commitment_messages(
                actor="evaluator",
                stage=envelope.stage,
                attempt=attempt,
                messages=[AIMessage(content=_json(review.model_dump()))],
            )
            return review

    async def run(
        self,
        envelope: TaskEnvelope,
        supervisor_messages: list[BaseMessage] | None = None,
    ) -> tuple[WorkerOutput | None, str]:
        supervisor_messages = supervisor_messages or []
        feedback = ""
        for attempt in range(1, _MAX_REVIEW_ATTEMPTS + 1):
            worker_feedback = feedback
            try:
                worker_output = await self._worker(
                    envelope,
                    worker_feedback,
                    supervisor_messages,
                    attempt,
                )
            except (ValidationError, _ModelOutputError) as exc:
                feedback = f"Worker输出不符合WorkerOutput结构：{exc}"
                if envelope.stage == 7:
                    feedback += "；阶段7必须返回result.contract_markdown"
                review = ReviewOutput(
                    approved=False,
                    feedback=feedback,
                    reasoning_summary="Worker输出结构无效，需要根据Schema修订后重试。",
                )
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[
                        HumanMessage(
                            content=_json(
                                {
                                    "supervisor_messages": [
                                        message.model_dump()
                                        for message in supervisor_messages
                                    ],
                                    "task": envelope.model_dump(),
                                    "reviewer_feedback": worker_feedback,
                                    "worker_output": None,
                                    "deterministic_validation_error": feedback,
                                }
                            )
                        ),
                        AIMessage(content=_json(review.model_dump())),
                    ],
                )
                continue
            structure_error = _validate_stage_result(
                envelope.stage,
                worker_output.result,
                supervisor_messages,
            )
            review = await self._evaluator(
                envelope,
                worker_output,
                supervisor_messages,
                attempt,
                structure_error or "",
                worker_feedback,
            )
            if review.approved:
                return worker_output, ""
            feedback = review.feedback or "Evaluator 未提供具体反馈"
        return None, feedback
