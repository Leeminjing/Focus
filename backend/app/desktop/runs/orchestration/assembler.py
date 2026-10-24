r"""本文件对外提供 RunExecutionAssembly、RunExecutionAssembler 与 RunAssemblySource 协议。

输入为已持久化 Run id 和只读装配 source；输出为可交给既有 execute_prepared_run 的 body、thread id 与 Agent factory。
具体工作流为 assembler 只验证 durable Run 状态并委托 source 从权威数据重建执行输入，既不领取 dispatch 也不启动任务。
示例：`assembly = await assembler.assemble("run-1")`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from langgraph.graph.state import CompiledStateGraph


@dataclass(frozen=True, slots=True)
class RunExecutionAssembly:
    run_id: str
    body: Any
    thread_id: str
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]]


class RunAssemblySource(Protocol):
    async def assemble_run(self, run_id: str) -> RunExecutionAssembly: ...


class RunExecutionAssembler:
    def __init__(self, source: RunAssemblySource) -> None:
        self._source = source

    async def assemble(self, run_id: str) -> RunExecutionAssembly:
        assembly = await self._source.assemble_run(run_id)
        if assembly.run_id != run_id:
            raise ValueError("装配结果与请求 Run identity 不一致")
        return assembly
