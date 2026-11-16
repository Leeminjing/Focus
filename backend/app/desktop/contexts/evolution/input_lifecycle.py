"""本文件对外提供 Context 输入释放通知与单一上层清理端口。

输入为调用方事务、已释放输入所属 Context 和组合根安装的异步处理器；输出为同事务完成的依赖清理。
具体工作流为组合根一次安装清理端口，仓储释放 owner 后通知该端口；失败向上传播以回滚释放。
本层不依赖清理处理器所属的 Patrol 或其他消费领域，也不提交调用方事务。
示例：set_context_input_release_handler(prune); await notify_context_inputs_released(session, context_id)。
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

ContextInputReleaseHandler = Callable[[AsyncSession, str], Awaitable[None]]
_handler: ContextInputReleaseHandler | None = None


def set_context_input_release_handler(handler: ContextInputReleaseHandler) -> None:
    global _handler
    _handler = handler


async def notify_context_inputs_released(
    session: AsyncSession, context_id: str
) -> None:
    """Cleanup runs inside the owner's transaction and retains failure atomicity."""
    if _handler is not None:
        await _handler(session, context_id)
