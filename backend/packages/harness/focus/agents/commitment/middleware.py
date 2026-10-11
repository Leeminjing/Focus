"""
本文件对外提供 CommitmentMiddleware，作为 lead agent 执行前的可拆卸承诺层入口。

输入:
    model — 承诺层内部 Worker 和 Evaluator 使用的 BaseChatModel。
    context7_tools_loader — 承诺阶段首次使用 Context7 时调用的异步工具加载函数。
    skill_names — 当前任务可用技能名集合，用于剥离消息前导的 /name skill token。
    AgentState / runtime — lead agent 当前消息状态和运行时信息；受治理安全上下文经
        runtime.context 携带，承诺子图的身份由它单调派生，不从扁平上下文搬运。

输出:
    None — 已存在 task_contract、没有消息或未显式输入 /commit 指令时跳过承诺层。
    dict[str, Any] — 同 checkpoint 追加独立合同／冻结引用与 typed authority，task_contract 为兼容视图；typed／legacy 来源显式保存。
    GraphInterrupt — 人工确认节点暂停时向父图传播的中断。

具体工作流:
    (1) abefore_agent 检查当前 thread 是否已经存在任务合同及有效 /commit 指令；
        消息前导的 skill token（如 /docx）先剥离再匹配，剥离的 token 不进入承诺任务文本。
    (2) 只把指令 HumanMessage（沿用触发消息 id）种子化隔离的九阶段子图；
        /commit 前置历史不进入子图，指令经 source_text 传递；阶段4 的上传清单
        优先取自 runtime.context 的 run_material_inputs（材料投影的服务端生产者），
        其次兼容指令文本中的 <current_uploads> 标签，经 uploads_tag 显式传递。
    (2.5) 承诺子图是本进程内的派生执行，由父级安全上下文单调派生自己的安全上下文
        （能力权限、工作根、访问模式都不放宽），子图以该上下文运行，落盘位置也取自它。
    (3) 按子图 checkpoint 存在性区分首次执行与 resume：首次从 stage 0 种子化；
        resume 时从父图 config 读取 resume 载荷并 Command(resume=...) 转发，
        子图从上次中断点继续，不重放已完成阶段。
    (4) 将子图 interrupt 原样传播给现有 run/resume 链路。
    (5) 子图完成后以精确耐久 checkpoint 编译批准合同与知识引用，完成子图可幂等重试交付。
    (6) 保留原输入，通过唯一 bridge 提交消息／Items；display 关系表达旧的替换式展示。
    (7) Supervisor 完整 messages 以 snapshot 模式沿原 custom 流发布，具名角色增量透传原输出身份。

示例:
    middleware = CommitmentMiddleware(model, load_context7_tools, skill_names)
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINTER,
    CONFIG_KEY_SCRATCHPAD,
)
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langgraph.graph.message import add_messages

from focus.agents.commitment.delegation import ReviewedDelegator
from focus.agents.commitment.handoff import compile_handoff
from focus.agents.commitment.schemas import CommitmentState
from focus.agents.commitment.tracing import _write_commitment_messages
from focus.agents.commitment.workflow import _build_supervisor
from focus.security.context import (
    ChildRole,
    derive_child_security_context,
    security_context_of,
)
from focus.security.governed import declare_governed_keys
from focus.history.bridge import synchronize_items
from focus.history.task_contract import task_contract_state_update

_SUBGRAPH_THREAD_SUFFIX = ":commitment"

declare_governed_keys("workspace", "thread_id")

_UPLOADS_TAG_RE = re.compile(
    r"<current_uploads>.*?</current_uploads>", re.DOTALL
)


def commitment_subgraph_thread_id(thread_id: str) -> str:
    return f"{thread_id}{_SUBGRAPH_THREAD_SUFFIX}"


def _uploads_tag(context: object) -> str:
    from focus.agents.material_inputs import RunMaterialInputs

    return RunMaterialInputs.from_context(context).uploads_tag


def _strip_leading_skill_tokens(text: str, skill_names: frozenset[str]) -> str:
    if not skill_names:
        return text
    rest = text
    while True:
        match = re.match(r"/(\S+)(?:\s+|$)", rest)
        if not match or match.group(1) not in skill_names:
            return rest
        rest = rest[match.end():].lstrip()


def _commit_instruction(
    message: Any,
    skill_names: frozenset[str] = frozenset(),
) -> str | None:
    if not isinstance(message, HumanMessage) or not isinstance(message.content, str):
        return None
    stripped = _strip_leading_skill_tokens(message.content.strip(), skill_names)
    match = re.fullmatch(r"/commit\s+(.+)", stripped, re.DOTALL)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_uploads_tag(instruction: str) -> tuple[str, str | None]:
    match = _UPLOADS_TAG_RE.search(instruction)
    if not match:
        return instruction, None
    tag = match.group(0).strip()
    cleaned = (instruction[: match.start()] + instruction[match.end() :]).strip()
    return cleaned, tag


def _parent_config() -> dict[str, Any]:
    try:
        return dict(get_config().get("configurable", {}))
    except RuntimeError:
        return {}


def _parent_checkpointer() -> Any | None:
    return _parent_config().get(CONFIG_KEY_CHECKPOINTER)


def _parent_resume_value() -> tuple[Any | None, bool]:
    conf = _parent_config()
    scratchpad = conf.get(CONFIG_KEY_SCRATCHPAD)
    if scratchpad is None:
        return None, False
    try:
        value = scratchpad.get_null_resume(False)
    except Exception:
        return None, False
    return value, value is not None


def _subgraph_config(thread_id: str) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": commitment_subgraph_thread_id(thread_id),
    }
    if checkpointer := _parent_checkpointer():
        configurable[CONFIG_KEY_CHECKPOINTER] = checkpointer
    return {"configurable": configurable}


def _seed_subgraph_input(
    trigger: HumanMessage,
    instruction: str,
    uploads_tag: str | None,
    thread_id: str,
    workspace: str,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "messages": [
            trigger.model_copy(update={"content": instruction}),
        ],
        "stage": 0,
        "awaiting_human": None,
        "artifacts": {},
        "thread_id": str(thread_id),
        "workspace": workspace,
        "knowledge_files": [],
        "knowledge_snapshots": [],
        "handoff_source_refs": source_refs or [],
        "source_text": instruction,
        "uploads_tag": uploads_tag or "",
    }


def _handoff_source_refs(context: dict, security) -> list[dict[str, Any]]:
    routing = security.routing
    refs = [{"kind": "commitment_run", "run_id": routing.run_id, "task_id": routing.task_id,
             "thread_id": routing.thread_id, "checkpoint_ns": routing.checkpoint_ns}]
    revision = context.get("context_revision_ref")
    if revision:
        refs.append({"kind": "context_revision", **(revision.model_dump(mode="json")
                                                   if hasattr(revision, "model_dump") else dict(revision))})
    return refs


async def _child_checkpoint(checkpointer, config):
    if checkpointer is None:
        raise RuntimeError("承诺合同交付需要可恢复的 child checkpointer")
    return await checkpointer.aget_tuple(config)


def _handoff_update(state, trigger, checkpoint):
    completed = checkpoint.checkpoint["channel_values"]
    configurable = checkpoint.config["configurable"]
    ref = {key: configurable.get(key, "") for key in ("thread_id", "checkpoint_ns", "checkpoint_id")}
    messages = compile_handoff(completed, trigger, ref, list(completed.get("handoff_source_refs", [])))
    combined = add_messages(state.get("messages", []), messages)
    return {"task_contract": completed["task_contract"], "task_contract_source": "typed", "messages": messages,
            "execution_items": synchronize_items(state.get("execution_items"), combined)}


class CommitmentMiddleware(AgentMiddleware):
    state_schema = CommitmentState
    _skill_names: frozenset[str] = frozenset()

    def __init__(
        self,
        model: BaseChatModel,
        context7_tools_loader: Callable[[], Awaitable[list[BaseTool]]],
        skill_names: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        delegator = ReviewedDelegator(
            model,
            context7_tools_loader=context7_tools_loader,
        )
        self._supervisor = _build_supervisor(delegator)
        self._skill_names = skill_names

    async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        mirror_update = task_contract_state_update(state)
        state = {**state, **mirror_update}
        if state.get("task_contract"):
            return mirror_update or None
        messages = state.get("messages", [])
        if not messages:
            return mirror_update or None
        trigger = messages[-1]
        instruction = _commit_instruction(trigger, self._skill_names)
        if instruction is None:
            return mirror_update or None
        if trigger.id is None:
            raise ValueError("/commit 触发消息缺少 message id")
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        if thread_id is None:
            raise ValueError("CommitmentMiddleware 无法获取 thread_id")
        instruction, tag = _extract_uploads_tag(instruction)
        runtime_context = getattr(runtime, "context", None)
        uploads_tag = _uploads_tag(runtime_context) or tag
        parent_security = security_context_of(runtime_context)
        child_security = derive_child_security_context(parent_security, ChildRole.COMMITMENT_WORKER)
        workspace = str(child_security.authorization.workspace)
        subgraph_config = _subgraph_config(str(thread_id))

        checkpointer = _parent_checkpointer()
        latest = await _child_checkpoint(checkpointer, subgraph_config)
        if latest is not None and latest.checkpoint["channel_values"].get("stage") == 9:
            return _handoff_update(state, trigger, latest)
        resume_value, is_resume = _parent_resume_value()
        subgraph_input: dict[str, Any] | Command
        if is_resume:
            if checkpointer is None or latest is None:
                raise RuntimeError(
                    "父图正在恢复承诺流程，但承诺子图 checkpoint 不可用；"
                    "无法从上次中断点继续"
                )
            subgraph_input = Command(resume=resume_value)
        else:
            if latest is not None:
                raise RuntimeError(
                    "承诺子图存在 checkpoint 但父图未处于 resume 状态；"
                    "拒绝静默重新从 stage 0 执行并丢弃人工决定"
                )
            subgraph_input = _seed_subgraph_input(
                trigger, instruction, uploads_tag, str(thread_id), workspace,
                _handoff_source_refs(runtime_context, parent_security),
            )

        result: dict[str, Any] = {}
        async for mode, chunk in self._supervisor.astream(
            subgraph_input,
            config=subgraph_config,
            context=child_security.to_runtime_context(),
            stream_mode=["values", "custom"],
            durability="sync",
        ):
            if mode == "custom":
                _write_commitment_messages(chunk)
            elif mode == "values":
                result = chunk
                _write_commitment_messages(
                    {
                        "type": "commitment_messages",
                        "actor": "supervisor",
                        "stage": int(chunk.get("stage", 0)),
                        "content_mode": "snapshot",
                        "messages": list(chunk.get("messages", [])),
                    }
                )
        if interrupts := result.get("__interrupt__"):
            raise GraphInterrupt(interrupts)
        if result.get("stage") != 9:
            raise RuntimeError(
                f"承诺流程异常终止于 stage {result.get('stage', 0)}"
            )
        completed = await _child_checkpoint(checkpointer, subgraph_config)
        if completed is None:
            raise RuntimeError("承诺流程缺少完成 checkpoint")
        return _handoff_update(state, trigger, completed)

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._run(state, runtime)
