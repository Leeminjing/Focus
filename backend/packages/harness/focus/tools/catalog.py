"""本文件对外提供 execution_pool_tools、compile_agent_tool_catalog 与只读 AgentToolCatalog。

输入为发现目录、普通工具和中间件声明；输出为带 owner 的原工具绑定与框架顺序一致的联合目录。
工作流为按声明来源选择执行池，排除插件桥拥有的插件，再在名称映射前校验中间件／普通工具的联合声明。
ToolBinding 保留 callable、metadata 和来源；本模块不加载插件、不执行工具、不决定权限或 Provider schema。
目录容器采用 tuple，消费者获得普通工具的新列表；重复名称即使引用同一对象也拒绝并报告双方位置。
示例：selected = execution_pool_tools(discovered, include_builtin=False)；catalog = compile_agent_tool_catalog(selected, middleware)。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool

from focus.tools.interfaces import ToolInfo


@dataclass(frozen=True)
class ToolBinding:
    tool: BaseTool
    owner: str
    source: str | None = None

    @property
    def name(self) -> str:
        return self.tool.name


@dataclass(frozen=True)
class AgentToolCatalog:
    regular_bindings: tuple[ToolBinding, ...]
    middleware_bindings: tuple[ToolBinding, ...]

    @property
    def bindings(self) -> tuple[ToolBinding, ...]:
        return (*self.middleware_bindings, *self.regular_bindings)

    @property
    def tools(self) -> tuple[BaseTool, ...]:
        return tuple(binding.tool for binding in self.bindings)

    @property
    def regular_tools(self) -> list[BaseTool]:
        return [binding.tool for binding in self.regular_bindings]


class ToolNameConflict(ValueError):
    def __init__(self, first: ToolBinding, second: ToolBinding):
        self.tool_name = first.name
        self.owners = (first.owner, second.owner)
        super().__init__(f"工具名称冲突: {first.name}; {first.owner} 与 {second.owner}")


def execution_pool_tools(discovered: Iterable[ToolInfo | BaseTool], *, include_builtin: bool = True) -> list[ToolBinding | BaseTool]:
    selected = []
    excluded = {"plugin"} if include_builtin else {"plugin", "builtin"}
    for position, entry in enumerate(discovered):
        if isinstance(entry, ToolInfo):
            if entry.source not in excluded:
                selected.append(ToolBinding(entry.tool(), f"pool:{entry.source}[{position}]", entry.source))
        else:
            selected.append(entry)
    return selected


def compile_agent_tool_catalog(regular_tools: Iterable[BaseTool | ToolBinding],
                               middleware: Iterable[AgentMiddleware]) -> AgentToolCatalog:
    regular = tuple(_binding(tool, f"tools[{position}]") for position, tool in enumerate(regular_tools))
    contributed = tuple(
        _binding(tool, f"middleware[{position}]:{type(owner).__name__}.tools[{index}]")
        for position, owner in enumerate(middleware)
        for index, tool in enumerate(getattr(owner, "tools", ()))
    )
    catalog = AgentToolCatalog(regular, contributed)
    seen: dict[str, ToolBinding] = {}
    for binding in catalog.bindings:
        if binding.name in seen:
            raise ToolNameConflict(seen[binding.name], binding)
        seen[binding.name] = binding
    return catalog


def _binding(tool: BaseTool | ToolBinding, owner: str) -> ToolBinding:
    binding = tool if isinstance(tool, ToolBinding) else ToolBinding(tool, owner)
    if not isinstance(binding.tool, BaseTool) or not isinstance(binding.name, str) or not binding.name:
        raise TypeError(f"工具目录需要命名 BaseTool: {binding.owner}")
    return binding
