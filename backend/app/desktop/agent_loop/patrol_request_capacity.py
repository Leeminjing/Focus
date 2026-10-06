"""本文件对外提供 PatrolRequestCapacity 的完整请求窗口检查。

输入为模型配置、授权单请求上限、实际消息与结构化 schema；输出为保守输入容量或明确 provider_request_window 异常。
具体工作流为复用 request_limits 扣除模型/授权输出预留，计算 UTF-8 请求和 schema 的 Token 上界；超窗在 Provider 调用和消费预留之前拒绝。
None 保持无额外限制；本模块不消费预算、不压缩正文、不修改来源或扩大模型容量。
示例：estimated = PatrolRequestCapacity(config, limits).check(messages, schema)。
"""

import json

from backend.app.desktop.agent_loop.context_expansion.candidate_paging import ProviderRequestWindowError
from backend.app.desktop.agent_loop.resource_limits import exceeds_limit, request_limits


class PatrolRequestCapacity:
    def __init__(self, config, limits: dict) -> None:
        policy = limits.get("expansion_resources") or {}
        self._limit, self.output_reserve = request_limits(
            policy.get("max_request_input_tokens"), policy.get("output_token_reserve"),
            config.context_window, config.curation_max_output_tokens,
        )

    def check(self, messages, schema) -> int:
        payload = {"messages": [{"role": message.type, "content": message.content} for message in messages],
                   "schema": schema.model_json_schema()}
        estimated = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 256
        if exceeds_limit(estimated, self._limit):
            raise ProviderRequestWindowError(
                f"Patrol provider_request_window: input_bound={estimated}, input_limit={self._limit}, output_reserve={self.output_reserve}",
                stage="patrol_decision",
            )
        return estimated
