"""本文件对外提供 ResponseChunkBridge，连接 typed SSE 与 LangChain 流消息。

输入为 Responses events/完成 decoder；输出为稳定 response 身份下的正文/公开 reasoning chunks 和唯一完整工具 chunk。
具体工作流为按 output/content index 合并可见文本，终态验证后附加原生 Items、usage 与调用参数，避免正文重放。
示例：for chunk in bridge.accept(event): yield chunk；for chunk in bridge.finish(): yield chunk。
"""

import json

from langchain_core.messages import AIMessageChunk

from focus.models.response_output import ResponseAttemptError, decode_response
from focus.models.response_stream import ResponseStreamAssembler


class ResponseChunkBridge:
    def __init__(self, contract, tools, *, model_name=None, source_manifest=None):
        self._contract = contract
        self._tools = tools
        self._assembler = ResponseStreamAssembler(contract.provider)
        self._texts = {}
        self._reasoning = ""
        self._open = set()
        self._finished = False
        self._model_name = model_name
        self._source_manifest = source_manifest

    def audit(self):
        return {**self._assembler.audit(), "request_source_manifest": self._source_manifest}

    def accept(self, event):
        channel, delta = self._assembler.accept(event)
        if event.get("type") == "response.output_item.added" and event.get("item", {}).get("type") == "message":
            index = event["output_index"] * 100000
            if index not in self._open:
                self._open.add(index)
                return [self._chunk([{"type": "text", "text": "", "index": index}])]
        if channel == "text" and delta:
            index = event["output_index"] * 100000 + event.get("content_index", 0)
            self._texts[index] = self._texts.get(index, "") + delta
            return [self._chunk([{"type": "text", "text": delta, "index": index}])]
        if channel == "reasoning" and delta:
            self._reasoning += delta
            return [self._chunk("", additional_kwargs={"reasoning_content": delta})]
        return []

    def finish(self):
        if self._finished:
            return []
        raw = self._assembler.finish()
        if self._model_name is not None:
            raw["requested_model"] = self._model_name
        if self._source_manifest is not None:
            raw["request_source_manifest"] = self._source_manifest
        message = decode_response(raw, self._contract, self._tools)
        result = []
        for output_index, item in enumerate(raw["output"]):
            if item.get("type") != "message":
                continue
            for content_index, block in enumerate(item.get("content", ())):
                if block.get("type") != "output_text":
                    continue
                index = output_index * 100000 + content_index
                displayed = self._texts.get(index, "")
                if not block["text"].startswith(displayed):
                    raise ResponseAttemptError("invalid", self.audit(), "已展示正文与最终输出不一致")
                remainder = block["text"][len(displayed):]
                if remainder:
                    result.append(self._chunk([{"type": "text", "text": remainder, "index": index}]))
        kwargs = dict(message.additional_kwargs)
        reasoning = kwargs.pop("reasoning_content", "")
        if reasoning and not self._reasoning:
            kwargs["reasoning_content"] = reasoning
        elif reasoning != self._reasoning:
            raise ResponseAttemptError("invalid", self.audit(), "已展示 reasoning 与最终输出不一致")
        calls = [{"name": call["name"], "id": call["id"], "args": json.dumps(call["args"], ensure_ascii=False),
                  "index": index, "type": "tool_call_chunk"} for index, call in enumerate(message.tool_calls)]
        result.append(self._chunk("", additional_kwargs=kwargs, response_metadata=message.response_metadata,
                                  usage_metadata=message.usage_metadata, tool_call_chunks=calls, chunk_position="last"))
        self._finished = True
        return result

    def _chunk(self, content, **kwargs):
        identity = self._assembler.response_id
        if not identity:
            raise ResponseAttemptError("invalid", self.audit(), "公开增量前缺少 response identity")
        return AIMessageChunk(content=content, id=f"response:{self._contract.provider}:{identity}", **kwargs)
