r"""本文件对外提供资源上限判断、剩余容量与模型请求容量计算。

输入为已用资源、可空预算上限及模型实际输入/输出容量；输出为是否越界、剩余额度或请求容量。
具体工作流为统一将 None 解释为不设额外限制，有限值继续执行原有预算检查，请求窗口取模型和用户限制中更严格的值。
示例：`remaining_capacity(None, used)` 保持不限；`request_limits(None, None, 128000, 8192)` 使用模型容量。
"""

from __future__ import annotations


def exceeds_limit(used: int, limit: int | None, *, inclusive: bool = False) -> bool:
    return limit is not None and (used >= limit if inclusive else used > limit)


def remaining_capacity(limit: int | None, used: int) -> int | None:
    return None if limit is None else max(0, limit - used)


def request_limits(
    input_limit: int | None,
    output_reserve: int | None,
    context_window: int | None,
    model_output: int,
) -> tuple[int | None, int]:
    output = max(output_reserve or 0, model_output)
    window_input = None if context_window is None else context_window - output
    limits = [value for value in (input_limit, window_input) if value is not None]
    return (min(limits) if limits else None), output
