"""本文件对外提供 response_source_manifest 的精确请求来源摘要。

输入为已投影实际请求、原始装配消息和 ProviderContract；输出为无凭据、无正文载荷的版本和内容哈希。
具体工作流为记录行为/工具/格式/请求哈希、冻结输入引用与预算，并保存 opaque replay 的实际有序前缀证明，
供模型 attempt 审计还原来源而不展开敏感内容。
示例：manifest = response_source_manifest(payload, messages, contract)。
"""

from dataclasses import asdict

from focus.history import content_hash
from focus.messages.request_budget import estimate_responses_budget
from focus.models.response_continuation import continuation_prefix


def response_source_manifest(payload, messages, contract):
    return {
        "provider_contract": asdict(contract), "requested_model": payload["model"],
        "request_hash": content_hash(payload), "instructions_hash": content_hash(payload.get("instructions")),
        "continuation_prefix": continuation_prefix(payload),
        "tools_hash": content_hash(payload.get("tools", [])), "format_hash": content_hash(payload.get("text")),
        "estimated_input_tokens": estimate_responses_budget(payload),
        "inputs": [{"message_id": message.id, "content_hash": content_hash(message.content),
                    "context": message.additional_kwargs.get("focus_context")} for message in messages],
    }
