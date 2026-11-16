"""本文件对外提供 Context 输入释放通知端口及幂等处理器注册。

输入为调用方事务、已释放输入所属 Context 和上层安装的异步处理器；输出为同事务完成的依赖清理。
具体工作流为组合根或索引存储注册具名处理器，仓储释放 owner 后通知已安装处理器；失败向上传播以回滚释放。
本层不依赖清理处理器所属的 Patrol 或其他消费领域，也不提交调用方事务。
示例：register_context_input_release_handler("index", prune); await notify_context_inputs_released(session, context_id)。
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

ContextInputReleaseHandler = Callable[[AsyncSession, str], Awaitable[None]]
_handlers: dict[str, ContextInputReleaseHandler] = {}


def register_context_input_release_handler(
    name: str, handler: ContextInputReleaseHandler
) -> None:
    """Install a named upper-layer cleanup port; repeated installation replaces it."""
    if not name:
        raise ValueError("Context input release handlers require a stable name")
    _handlers[name] = handler


async def notify_context_inputs_released(
    session: AsyncSession, context_id: str
) -> None:
    """Cleanup runs inside the owner's transaction and retains failure atomicity."""
    for handler in tuple(_handlers.values()):
        await handler(session, context_id)
