"""本文件对外提供 WaitSeconds、MessageKind 和 validate_wait_seconds。

输入为协作工具等待秒数或消息类型；输出为同时用于模型 schema 与运行验证的受约束参数。
具体工作流为使用同一类型定义公开范围/枚举，直接调用也通过类型适配器验证，避免隐藏约束。
示例：seconds = validate_wait_seconds(30)，MessageKind 仅接受已支持的协议类型。
"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

WaitSeconds = Annotated[int, Field(ge=1, le=30)]
MessageKind = Literal["message", "plan_approval_request", "plan_approval_response", "shutdown_request", "shutdown_response"]
_WAIT_SECONDS = TypeAdapter(WaitSeconds)


def validate_wait_seconds(value: int) -> int:
    return _WAIT_SECONDS.validate_python(value)
