"""本文件对外提供 ResponseAttemptError、decode_response 与 response_text。

输入为 Provider 完整 Responses 对象和当前工具 schema；输出为保留全部原生 Items 的 LangGraph AIMessage。
具体工作流为核验 completed、拒绝与调用身份/参数，工具名称和 JSON schema 通过后才暴露可执行 calls。
错误携带独立 attempt audit，不进入普通完成历史；缺失 usage 在原始元数据中保持 unknown。
示例：message = decode_response(raw, contract, tools)；response_text(message) 只读取助手正文。
"""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage

class ResponseAttemptError(RuntimeError):
    def __init__(self, status: str, audit: dict, detail: str):
        super().__init__(f"Responses attempt {status}: {detail}")
        self.status = status
        self.audit = deepcopy(audit)


def response_text(message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(block if isinstance(block, str) else block.get("text", "")
                   for block in message.content if isinstance(block, str) or block.get("type") in {"text", "output_text"})


def decode_response(raw: dict, contract, tools: list[dict]) -> AIMessage:
    status = raw.get("status")
    if status != "completed":
        raise ResponseAttemptError(status or "unknown", raw, "Provider 未返回有效 completed 终态")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ResponseAttemptError("invalid", raw, "output 必须为 typed Item 数组")
    try:
        calls, text, reasoning = _output_fields(output, tools, contract.provider)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResponseAttemptError("invalid", raw, str(exc)) from exc
    if any(block.get("type") == "refusal" for item in output if item.get("type") == "message" for block in item.get("content", ())):
        raise ResponseAttemptError("refusal", raw, "Provider 拒绝完成请求")
    identity = raw.get("id")
    if not isinstance(identity, str) or not identity:
        raise ResponseAttemptError("invalid", raw, "response identity 缺失")
    metadata = {key: deepcopy(value) for key, value in raw.items() if key != "output"}
    metadata.update(provider=contract.provider, protocol=contract.protocol, projection_version=contract.projection_version,
                    model_name=raw.get("model"), finish_reason="tool_calls" if calls else "stop")
    kwargs = {"focus_response_items": deepcopy(output)}
    if reasoning:
        kwargs["reasoning_content"] = reasoning
    return AIMessage(content=text, id=f"response:{contract.provider}:{identity}", tool_calls=calls,
                     additional_kwargs=kwargs, response_metadata=metadata, usage_metadata=_usage(raw.get("usage")))


def _output_fields(output, tools, provider):
    schemas = {tool["name"]: tool.get("parameters", {"type": "object"}) for tool in tools}
    identities = set()
    call_ids = set()
    calls, texts, reasonings = [], [], []
    for item in output:
        identity = item.get("id")
        if identity is not None:
            if identity in identities:
                raise ValueError("原生 Item identity 重复")
            identities.add(identity)
        if item.get("status") not in {None, "completed"}:
            raise ValueError("completed 响应包含未完成 Item")
        kind = item.get("type")
        if kind == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id or call_id in call_ids or name not in schemas:
                raise ValueError("工具调用 identity/name 不属于当前暴露合同")
            call_ids.add(call_id)
            args = json.loads(item["arguments"])
            if not isinstance(args, dict):
                raise ValueError("工具 arguments 必须为 JSON object")
            errors = list(Draft202012Validator(schemas[name]).iter_errors(args))
            if errors:
                raise ValueError("工具 arguments 不符合当前 schema")
            calls.append({"id": call_id, "name": name, "args": args, "type": "tool_call"})
        elif kind == "message":
            if item.get("role") != "assistant":
                raise ValueError("Provider 输出 role 非 assistant")
            for block in item.get("content", ()):
                if block.get("type") == "output_text":
                    texts.append(block["text"])
                elif block.get("type") != "refusal":
                    raise ValueError("Provider 输出模态缺少能力合同")
        elif kind == "reasoning":
            field, block_type = ("content", "reasoning_text") if provider == "deepseek" else ("summary", "summary_text")
            reasonings.extend(block["text"] for block in item.get(field, ()) if block.get("type") == block_type)
        elif kind in {"custom_tool_call", "web_search_call", "file_search_call", "computer_call", "mcp_call", "image_generation_call"}:
            raise ValueError("收到未声明的 hosted/custom tool 调用")
    return calls, "".join(texts), "".join(reasonings)


def _usage(raw):
    if not isinstance(raw, dict) or not all(isinstance(raw.get(key), int) and raw[key] >= 0
                                          for key in ("input_tokens", "output_tokens", "total_tokens")):
        return None
    result = {key: raw[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
    for raw_key, key, detail_name in (("input_tokens_details", "input_token_details", "cached_tokens"),
                                      ("output_tokens_details", "output_token_details", "reasoning_tokens")):
        value = (raw.get(raw_key) or {}).get(detail_name)
        if isinstance(value, int) and value >= 0:
            result[key] = {"cache_read" if detail_name == "cached_tokens" else "reasoning": value}
    return result
