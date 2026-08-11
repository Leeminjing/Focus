"""
本文件对外提供 CommitmentMiddleware，作为 lead agent 执行前的可拆卸承诺层入口。

输入:
    model — 承诺层内部 Worker 和 Evaluator 使用的 BaseChatModel。
    context7_tools — 只提供给承诺子图的已加载 Context7 BaseTool 列表。
    skill_names — 当前任务可用技能名集合，用于剥离消息前导的 /name skill token。
    AgentState / runtime — lead agent 当前消息状态和包含 thread_id 的运行时信息。

输出:
    None — 已存在 task_contract、没有消息或未显式输入 /commit 指令时跳过承诺层。
    dict[str, Any] — 完成后写入 task_contract，并原位替换 /commit 消息。
    GraphInterrupt — 人工确认节点暂停时向父图传播的中断。

具体工作流:
    (1) abefore_agent 检查当前 thread 是否已经存在任务合同及有效 /commit 指令；
        消息前导的 skill token（如 /docx）先剥离再匹配，剥离的 token 不进入承诺任务文本。
    (2) 只把指令 HumanMessage（沿用触发消息 id）种子化隔离的九阶段子图；
        /commit 前置历史不进入子图，指令经 source_text 传递；阶段4 的上传清单
        来自 runtime.context 的 uploads（桌面材料系统）或指令文本中的
        <current_uploads> 标签，经 uploads_tag 显式传递。
    (3) 按子图 checkpoint 存在性区分首次执行与 resume：首次从 stage 0 种子化；
        resume 时从父图 config 读取 resume 载荷并 Command(resume=...) 转发，
        子图从上次中断点继续，不重放已完成阶段。
    (4) 将子图 interrupt 原样传播给现有 run/resume 链路。
    (5) 子图完成后校验 stage、合同和最终消息。
    (6) 返回状态更新，由 LangGraph reducer 以相同 id 原位写入合同 HumanMessage。

示例:
    middleware = CommitmentMiddleware(model, context7_tools, skill_names)
"""

import asyncio
import re
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

from focus.agents.commitment.delegation import ReviewedDelegator
from focus.agents.commitment.schemas import CommitmentState
from focus.agents.commitment.tracing import _write_commitment_messages
from focus.agents.commitment.workflow import _build_supervisor

_SUBGRAPH_THREAD_SUFFIX = ":commitment"

_UPLOADS_TAG_RE = re.compile(
    r"<current_uploads>.*?</current_uploads>", re.DOTALL
)


def commitment_subgraph_thread_id(thread_id: str) -> str:
    """返回承诺子图使用的派生 thread id。"""
    return f"{thread_id}{_SUBGRAPH_THREAD_SUFFIX}"


def _strip_leading_skill_tokens(text: str, skill_names: frozenset[str]) -> str:
    """剥离文本前导的 /<skill-name> token 序列，返回剩余文本。

    输入:
        text: str — 去除外围空白后的消息文本
        skill_names: frozenset[str] — 当前任务可用技能名集合

    输出:
        str — 剥离前导 skill token 后的文本；无 token 或集合为空时原样返回

    工作流:
        (1) 从文本首部逐 token 匹配 /<skill-name>（精确、大小写敏感）。
        (2) 连续匹配的 token 全部剥离，遇到第一个不匹配 token 停止并返回剩余文本。
    """
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
    """从指令文本中分离 <current_uploads> 标签块。

    输入:
        instruction: str — 去掉 /commit 前缀后的指令文本。

    输出:
        tuple[str, str | None] — (剥离标签后的指令, 标签块原文或 None)。

    工作流:
        (1) 用非贪婪正则查找 <current_uploads>...</current_uploads> 块。
        (2) 找到则从指令中移除并返回标签原文；未找到返回指令原样。

    示例:
        clean, tag = _extract_uploads_tag("做X\\n\\n<current_uploads>...</current_uploads>")
    """
    match = _UPLOADS_TAG_RE.search(instruction)
    if not match:
        return instruction, None
    tag = match.group(0).strip()
    cleaned = (instruction[: match.start()] + instruction[match.end() :]).strip()
    return cleaned, tag


def _parent_config() -> dict[str, Any]:
    """读取当前节点执行的 configurable；不在节点上下文时返回空 dict。"""
    try:
        return dict(get_config().get("configurable", {}))
    except RuntimeError:
        return {}


def _parent_checkpointer() -> Any | None:
    """获取父图运行期挂载的 checkpointer 实例（经 config 注入）。

    实现依据: langgraph pregel 在节点 configurable 中注入
    CONFIG_KEY_CHECKPOINTER。
    """
    return _parent_config().get(CONFIG_KEY_CHECKPOINTER)


def _parent_resume_value() -> tuple[Any | None, bool]:
    """不消费地探测父图是否处于 resume 重执行，并返回 resume 载荷。

    输出:
        tuple[Any | None, bool] — (resume 值或 None, 是否处于 resume 重执行)。

    工作流:
        (1) 读取 configurable 中的 CONFIG_KEY_SCRATCHPAD。
        (2) get_null_resume(consume=False) 只探测不消费。
        (3) 不在节点上下文或无法读取时返回 (None, False)。
    """
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
    """构造承诺子图的隔离 checkpoint 配置。

    使用 thread_id 派生的隔离命名空间（{thread_id}:commitment），与父图 checkpoint
    键互不冲突；父图 checkpointer 经 CONFIG_KEY_CHECKPOINTER 注入，使子图在自身
    未挂 checkpointer 的情况下仍可持久化与恢复。
    """
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
) -> dict[str, Any]:
    """构造子图首次执行的种子输入：只含指令消息，不携带 /commit 前置历史。"""
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
        "source_text": instruction,
        "uploads_tag": uploads_tag or "",
    }


class CommitmentMiddleware(AgentMiddleware):
    state_schema = CommitmentState
    _skill_names: frozenset[str] = frozenset()

    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
        skill_names: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        delegator = ReviewedDelegator(model, context7_tools)
        self._supervisor = _build_supervisor(delegator)
        self._skill_names = skill_names

    async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        if state.get("task_contract"):
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        trigger = messages[-1]
        instruction = _commit_instruction(trigger, self._skill_names)
        if instruction is None:
            return None
        if trigger.id is None:
            raise ValueError("/commit 触发消息缺少 message id")
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        if thread_id is None:
            raise ValueError("CommitmentMiddleware 无法获取 thread_id")
        instruction, tag = _extract_uploads_tag(instruction)
        # 本土化：阶段4 上传清单优先来自 langgraph_context（桌面材料系统注入），
        # 其次兼容指令文本中的 <current_uploads> 标签。
        uploads_tag = tag
        runtime_context = getattr(runtime, "context", None)
        if isinstance(runtime_context, dict) and runtime_context.get("uploads"):
            uploads_tag = str(runtime_context["uploads"])
        workspace = ""
        if isinstance(runtime_context, dict):
            workspace = str(runtime_context.get("workspace", ""))
        if not workspace:
            raise ValueError("CommitmentMiddleware 无法获取 workspace")
        subgraph_config = _subgraph_config(str(thread_id))

        # 区分首次执行与 resume：父图 resume 重执行时 config 携带 resume 载荷，
        # 子图 checkpoint 必须与之对应存在；任一方向不一致都显式报错，不静默重放。
        checkpointer = _parent_checkpointer()
        resume_value, is_resume = _parent_resume_value()
        subgraph_input: dict[str, Any] | Command
        if is_resume:
            latest = None
            if checkpointer is not None:
                try:
                    latest = await checkpointer.aget_tuple(subgraph_config)
                except Exception:
                    latest = None
            if checkpointer is None or latest is None:
                raise RuntimeError(
                    "父图正在恢复承诺流程，但承诺子图 checkpoint 不可用；"
                    "无法从上次中断点继续"
                )
            subgraph_input = Command(resume=resume_value)
        else:
            if checkpointer is not None:
                try:
                    latest = await checkpointer.aget_tuple(subgraph_config)
                except Exception:
                    latest = None
                if latest is not None:
                    raise RuntimeError(
                        "承诺子图存在 checkpoint 但父图未处于 resume 状态；"
                        "拒绝静默重新从 stage 0 执行并丢弃人工决定"
                    )
            subgraph_input = _seed_subgraph_input(
                trigger, instruction, uploads_tag, str(thread_id), workspace
            )

        result: dict[str, Any] = {}
        async for mode, chunk in self._supervisor.astream(
            subgraph_input,
            config=subgraph_config,
            stream_mode=["values", "custom"],
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
                        "messages": list(chunk.get("messages", [])),
                    }
                )
        if interrupts := result.get("__interrupt__"):
            raise GraphInterrupt(interrupts)
        if result.get("stage") != 9:
            raise RuntimeError(
                f"承诺流程异常终止于 stage {result.get('stage', 0)}"
            )
        contract = str(result.get("task_contract", ""))
        final_message = str(result.get("final_message", ""))
        if not contract or not final_message:
            raise RuntimeError("承诺流程未产出合同或最终消息")
        return {
            "task_contract": contract,
            "messages": [
                HumanMessage(content=final_message, id=trigger.id),
            ],
        }

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._run(state, runtime)
