"""本文件对外提供 ResponseStreamAssembler 的 typed SSE 收束。

输入为按 Provider 序列接收的事件；输出为分离 text/reasoning 增量与经过 terminal 校验的完整响应。
具体工作流为按 sequence/output/content identity 去重与组装，验证 done/terminal 一致性；参数不流入展示通道。
只有 finish 获得 completed 才能交给调用 decoder；无终态断流产生带 partial audit 的错误。
示例：channel, delta = assembler.accept(event)；raw = assembler.finish()。
"""

from copy import deepcopy

from focus.history import content_hash
from focus.models.response_output import ResponseAttemptError


class ResponseStreamAssembler:
    def __init__(self, provider: str):
        self._provider = provider
        self._events = {}
        self._items = {}
        self._buffers = {}
        self._done = set()
        self._terminal = None
        self._response_id = None

    @property
    def response_id(self):
        return self._response_id

    def audit(self):
        return {"status": "partial", "response_id": self._response_id,
                "items": deepcopy(self._items), "buffers": deepcopy(self._buffers)}

    def accept(self, event: dict):
        sequence = event.get("sequence_number")
        digest = content_hash(event)
        if sequence is not None:
            if sequence in self._events:
                if self._events[sequence] != digest:
                    self._fail("同一 sequence 的事件内容冲突")
                return None, ""
            self._events[sequence] = digest
        kind = event.get("type", "")
        if kind.startswith("response.") and isinstance(event.get("response"), dict):
            identity = event["response"].get("id")
            if identity is not None:
                if self._response_id not in {None, identity}:
                    self._fail("response identity 冲突")
                self._response_id = identity
        if kind in {"response.completed", "response.incomplete", "response.failed", "error"}:
            self._settle(kind, event)
            return None, ""
        if self._terminal is not None:
            self._fail("终态之后出现新输出事件")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            self._item(event, kind.endswith(".done"))
            return None, ""
        if kind.endswith(".delta"):
            return self._delta(event)
        if kind in {"response.function_call_arguments.done", "response.output_text.done", "response.reasoning_text.done", "response.reasoning_summary_text.done"}:
            key = self._buffer_key(event)
            expected = event.get("arguments", event.get("text", ""))
            if key in self._buffers and self._buffers[key] != expected:
                self._fail("done 与已展示/组装的 delta 不一致")
        return None, ""

    def finish(self):
        if self._terminal is None:
            raise ResponseAttemptError("disconnected", self.audit(), "流结束但无有效 terminal")
        if self._terminal.get("status") != "completed":
            raise ResponseAttemptError(self._terminal.get("status", "failed"), self._terminal, "Provider 未完成请求")
        raw = deepcopy(self._terminal)
        if "output" not in raw:
            if set(self._items) != self._done:
                self._fail("terminal 缺少完整 output 且 Items 未闭合")
            raw["output"] = [self._items[index] for index in sorted(self._items)]
        for index, item in enumerate(raw["output"]):
            saved = self._items.get(index)
            if saved and (saved.get("id") != item.get("id") or saved.get("type") != item.get("type")):
                self._fail("terminal output identity 与流不一致")
            if index in self._done and saved != item:
                self._fail("terminal output 与已完成 Item 不一致")
            if item.get("type") == "function_call":
                values = [value for key, value in self._buffers.items() if key.startswith(f"{index}:response.function_call_arguments:")]
                if values and values != [item.get("arguments")]:
                    self._fail("最终调用参数与 delta 不一致")
        if self._items and len(raw["output"]) != len(self._items):
            self._fail("terminal output 丢失流式 Items")
        return raw

    def _item(self, event, done):
        index, item = event["output_index"], deepcopy(event["item"])
        previous = self._items.get(index)
        if previous and any(previous.get(key) != item.get(key) for key in ("id", "type", "call_id", "name")):
            self._fail("output Item identity 或调用关联冲突")
        if index in self._done:
            if previous != item:
                self._fail("已完成 Item 被改写")
            return
        self._items[index] = item
        if done:
            self._done.add(index)

    def _delta(self, event):
        kind = event["type"]
        index = event.get("output_index")
        item = self._items.get(index)
        if item is None or item.get("id") != event.get("item_id") or index in self._done:
            self._fail("delta 缺少合法开放 Item")
        channel = None
        if kind == "response.output_text.delta":
            if item.get("type") != "message" or item.get("role") != "assistant":
                self._fail("text delta 来源不是助手消息")
            channel = "text"
        elif kind == "response.reasoning_summary_text.delta" and self._provider == "openai":
            if item.get("type") != "reasoning":
                self._fail("summary delta 来源非 reasoning")
            channel = "reasoning"
        elif kind == "response.reasoning_text.delta" and self._provider == "deepseek":
            if item.get("type") != "reasoning":
                self._fail("reasoning delta 来源非 reasoning")
            channel = "reasoning"
        elif kind == "response.function_call_arguments.delta":
            if item.get("type") != "function_call":
                self._fail("arguments delta 来源非 function call")
        else:
            return None, ""
        key = self._buffer_key(event)
        delta = event.get("delta", "")
        if not isinstance(delta, str):
            self._fail("delta 必须为文本")
        self._buffers[key] = self._buffers.get(key, "") + delta
        return channel, delta if channel else ""

    @staticmethod
    def _buffer_key(event):
        kind = event["type"].rsplit(".", 1)[0]
        return f"{event.get('output_index')}:{kind}:{event.get('content_index', event.get('summary_index', 0))}"

    def _settle(self, kind, event):
        response = deepcopy(event.get("response", {"status": "failed", "error": event}))
        status = kind.removeprefix("response.") if kind != "error" else "failed"
        if response.get("status") != status:
            self._fail("terminal event 与 response status 不一致")
        if self._terminal is not None and self._terminal != response:
            self._fail("terminal 重放冲突")
        self._terminal = response

    def _fail(self, detail):
        raise ResponseAttemptError("invalid", self.audit(), detail)
