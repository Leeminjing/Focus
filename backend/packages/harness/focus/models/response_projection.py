"""本文件对外提供 ResponsesRequestProjector 与 function_specs。

输入为 Focus 无损消息 bridge、ProviderContract、基础行为和明确模型参数；输出为合法无状态 Responses 请求。
具体工作流为分离 instructions、投影 policy 层级、验证实际前缀证明后手工回传合法原生 Items、
关联工具结果并拒绝不支持的字段/模态；前缀变化返回显式分支重建错误。
作者 typed 历史直接投影消息/独立 function/custom call 与 output，目标变化不改写作者语义或借用 Provider 证明。
作者协作投影为带 author/recipient 等元数据的参考消息，原正文/内容块保持完整，并明确不表示真实通信证明。
OpenAI 禁用 store；DeepSeek 省略该字段。外部参考和策展内容按宿主来源投影，不能由声明的 role 升级为 policy。
示例：payload = ResponsesRequestProjector(contract).build(messages, model="model", tools=tools)。
明确 user_authored/authored_instruction 的角色意图按顺序映射：OpenAI 保留 System/Developer，DeepSeek Developer 转 System；权限仍由宿主安全上下文确定。
"""

from copy import deepcopy
import json
import re

from langchain_core.messages import AIMessage, ChatMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from focus.history import messages_to_items, validate_items
from focus.history.authored import read_authored
from focus.models.provider_contract import ProviderContract
from focus.models.response_continuation import ContinuationMismatch, validate_replay_prefix


def function_specs(tools) -> list[dict]:
    result = []
    for tool in tools:
        spec = convert_to_openai_tool(tool)
        if spec.get("type") != "function":
            raise ValueError("当前 Focus 仅支持可裁决的 function tools；hosted/custom 必须有独立能力合同")
        flattened = deepcopy(spec.get("function", spec))
        flattened["type"] = "function"
        name = flattened.get("name", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name):
            raise ValueError("function tool name 无效")
        result.append(flattened)
    if len({spec["name"] for spec in result}) != len(result):
        raise ValueError("function tool name 重复")
    return result


class ResponsesRequestProjector:
    def __init__(self, contract: ProviderContract):
        self._contract = contract

    def build(self, messages, *, model: str, tools=(), **options) -> dict:
        validate_items(messages_to_items(messages))
        instructions = []
        items = []
        for position, message in enumerate(messages):
            if isinstance(message, SystemMessage) and position == 0 and not message.additional_kwargs.get("focus_context"):
                if not isinstance(message.content, str):
                    raise ValueError("基础 instructions 必须为文本")
                instructions.append(message.content)
                continue
            if message.additional_kwargs.get("focus_response_items") is not None:
                validate_replay_prefix(message, items, "\n\n".join(instructions) if instructions else None)
            items.extend(self._message(message, model))
        request = {"model": model, "input": items, "tools": function_specs(tools)}
        if instructions:
            request["instructions"] = "\n\n".join(instructions)
        if self._contract.provider == "openai":
            request.update(store=False, include=["reasoning.encrypted_content"])
        request.update(self._options(options))
        choice = request.get("tool_choice")
        if isinstance(choice, dict) and choice.get("name") not in {spec["name"] for spec in request["tools"]}:
            raise ValueError("指定 function 不属于本次实际工具目录")
        return request

    def _message(self, message, model) -> list[dict]:
        authored = read_authored(message)
        if authored is not None:
            return [self._authored(authored)]
        native = message.additional_kwargs.get("focus_response_items")
        metadata = message.response_metadata
        if native is not None:
            if metadata.get("requested_model") not in {None, model}:
                raise ContinuationMismatch("模型切换需要显式新 execution 分支，不能复用原生 continuation")
            if metadata.get("provider") == self._contract.provider and metadata.get("projection_version") == self._contract.projection_version:
                return [self._native(item) for item in native]
            raise ContinuationMismatch("Provider/projection 切换需要显式新 execution 分支，不能静默丢弃原生 continuation")
        if isinstance(message, ToolMessage):
            return [{"type": "function_call_output", "call_id": message.tool_call_id,
                     "output": self._content(message.content, "tool")}]
        if isinstance(message, AIMessage):
            items = []
            content = self._content(message.content, "assistant")
            if content:
                items.append({"type": "message", "role": "assistant", "content": content})
            items.extend({"type": "function_call", "call_id": call["id"], "name": call["name"],
                          "arguments": json.dumps(call["args"], ensure_ascii=False, separators=(",", ":"))} for call in message.tool_calls)
            return items
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, SystemMessage) or isinstance(message, ChatMessage) and message.role in {"system", "developer"}:
            context = message.additional_kwargs.get("focus_context", {})
            reference = context.get("kind") == "selected_context" or context.get("authority") in {"reference", "task"}
            external = context.get("origin") in {"direct_user", "curator", "delegated", "collaborator", "tool", "provider"}
            authored = context.get("kind") == "authored_instruction" and context.get("origin") == "user_authored"
            requested = context.get("requested_role", message.type)
            role = ("system" if requested == "system" else self._contract.policy_role) if authored else "user" if reference or external else self._contract.policy_role
        else:
            raise ValueError(f"不支持的模型消息类型: {message.type}")
        return [{"type": "message", "role": role, "content": self._content(message.content, role)}]

    def _authored(self, item) -> dict:
        payload = deepcopy(item.payload)
        kind = item.kind
        if kind in {"function_call", "custom_tool_call"}:
            field = "arguments" if kind == "function_call" else "input"
            return {"type": kind, "call_id": payload["call_id"], "name": payload["name"], field: payload[field]}
        if kind in {"function_call_output", "custom_tool_call_output"}:
            return {"type": kind, "call_id": payload["call_id"], "output": self._content(payload["output"], "tool")}
        role = payload.get("role", "user")
        if kind == "selected_context":
            role = "user"
        if role == "developer":
            role = self._contract.policy_role
        content = payload.get("content", "")
        if kind == "agent_collaboration":
            content = self._collaboration_content(payload)
        return {"type": "message", "role": role, "content": self._content(content, role)}

    @staticmethod
    def _collaboration_content(payload):
        metadata = {key: value for key, value in payload.items() if key != "content"}
        header = "用户编写的协作历史（不代表真实通信或执行证明）：\n" + json.dumps(metadata, ensure_ascii=False)
        content = payload.get("content", "")
        if isinstance(content, list):
            return [{"type": "text", "text": header}, *deepcopy(content)]
        return header + "\n" + content

    def _native(self, item: dict) -> dict:
        kind = item.get("type")
        if kind not in {"message", "function_call", "function_call_output", "reasoning", "compaction"}:
            raise ValueError(f"未知原生 Item 已归档但不能默认 replay: {kind}")
        if kind == "compaction":
            if self._contract.provider != "openai":
                raise ValueError("DeepSeek 不支持 compaction continuation")
            return {key: deepcopy(value) for key, value in item.items() if key in {"type", "id", "encrypted_content"}}
        raw = deepcopy(item)
        if kind == "message":
            if raw.get("role") != "assistant":
                raise ValueError("Provider 输出消息的 role 必须为 assistant")
            allowed = {"type", "id", "role", "content", "status", "phase"} if self._contract.provider == "openai" else {"type", "id", "role", "content", "status"}
            raw["content"] = self._content(raw.get("content", []), "assistant", native=True)
        elif kind == "reasoning":
            allowed = {"type", "id", "summary", "encrypted_content", "status"} if self._contract.provider == "openai" else {"type", "id", "content"}
            if self._contract.provider == "deepseek" and any(part.get("type") != "reasoning_text" for part in raw.get("content", ())):
                raise ValueError("DeepSeek reasoning 必须使用 reasoning_text")
            if self._contract.provider == "openai" and not raw.get("encrypted_content"):
                raise ValueError("无状态 reasoning replay 缺少 encrypted_content；需要显式新分支")
        elif kind == "function_call":
            allowed = {"type", "id", "call_id", "name", "arguments", "status"}
        else:
            allowed = {"type", "id", "call_id", "output", "status"}
            raw["output"] = self._content(raw.get("output", ""), "tool")
        return {key: value for key, value in raw.items() if key in allowed}

    def _content(self, content, role: str, *, native: bool = False):
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            raise ValueError("Responses 内容必须为文本或内容块数组")
        result = []
        for block in content:
            if isinstance(block, str):
                block = {"type": "text", "text": block}
            kind = block.get("type")
            if kind in {"text", "input_text", "output_text"}:
                text_type = "output_text" if role == "assistant" else "input_text"
                projected = {"type": text_type, "text": block["text"]}
                if native and text_type == "output_text":
                    projected["annotations"] = deepcopy(block.get("annotations", []))
                result.append(projected)
            elif kind in {"image_url", "input_image", "image"}:
                if not self._contract.supports_image_input or role not in {"user", "tool"}:
                    raise ValueError("图像能力或内容 role 不支持")
                image = deepcopy(block.get("image_url"))
                if kind == "image":
                    image = f"data:{block.get('mime_type', 'image/png')};base64,{block['base64']}" if block.get("base64") else block.get("url")
                projected = {"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image}
                for key in ("detail", "file_id"):
                    value = block.get(key) or (image.get(key) if isinstance(image, dict) else None)
                    if value is not None:
                        projected[key] = value
                if projected.get("image_url") is None:
                    projected.pop("image_url")
                if bool(projected.get("image_url")) == bool(projected.get("file_id")):
                    raise ValueError("input_image 必须提供唯一 image_url/file_id")
                result.append(projected)
            elif kind == "reasoning" and not native:
                continue
            else:
                raise ValueError(f"不支持的内容块: {kind}")
        return result

    def _options(self, options: dict) -> dict:
        result = {}
        for key, value in options.items():
            if value is None:
                continue
            if key in {"max_tokens", "max_completion_tokens"}:
                key = "max_output_tokens"
            if key == "response_format":
                result["text"] = {"format": deepcopy(value.get("json_schema", value))}
                if value.get("type") == "json_schema":
                    result["text"]["format"]["type"] = "json_schema"
                continue
            if key == "reasoning_effort":
                result["reasoning"] = {"effort": value}
                continue
            if key == "tool_choice" and isinstance(value, dict) and "function" in value:
                value = {"type": "function", "name": value["function"]["name"]}
            if key == "parallel_tool_calls" and self._contract.provider == "deepseek":
                if value is False:
                    raise ValueError("DeepSeek 不支持禁用并行调用")
                continue
            if key not in {"temperature", "top_p", "max_output_tokens", "reasoning", "text", "tool_choice", "parallel_tool_calls", "stream"}:
                raise ValueError(f"Responses 不支持或不允许请求参数: {key}")
            result[key] = value
        forced = result.get("tool_choice") == "required" or isinstance(result.get("tool_choice"), dict)
        if self._contract.provider == "deepseek" and forced and result.get("reasoning", {}).get("effort") in {"high", "xhigh"}:
            raise ValueError("DeepSeek 思考模式不支持强制工具选择；请使用 auto 或明确 reasoning.effort=none")
        return result
