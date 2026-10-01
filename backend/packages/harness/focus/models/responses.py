"""本文件对外提供 FocusResponsesChatModel，作为 LangGraph 的 Responses 模型 bridge。

输入为显式 ProviderContract、模型凭据、无损 messages 和请求选项；输出为完整 AIMessage 或 typed stream chunks。
具体工作流为独立 projector 构造无状态请求、首次实际调用才创建相应 SDK 客户端、decoder 验证终态/工具并保存原生 Items。
结构化输出使用 text.format，仍由领域 Pydantic schema 校验；本类不拥有 Context/Revision 或工具执行权。
示例：model = FocusResponsesChatModel(model="model", api_key="...", provider_contract=contract)；await model.ainvoke(messages)。
"""

import asyncio
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field, PrivateAttr, SecretStr

from focus.models.provider_contract import ProviderContract
from focus.models.response_chunks import ResponseChunkBridge
from focus.models.response_output import ResponseAttemptError, decode_response, response_text
from focus.models.response_projection import ResponsesRequestProjector, function_specs
from focus.messages.request_budget import estimate_responses_budget
from focus.models.response_source import response_source_manifest
from focus.models.response_clients import responses_clients


class FocusResponsesChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True, extra="forbid")

    model_name: str = Field(alias="model")
    api_key: SecretStr = Field(exclude=True)
    base_url: str | None = None
    provider_contract: ProviderContract
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning: dict | None = None
    text: dict | None = None
    streaming: bool = False
    max_retries: int = Field(default=0, ge=0, le=2)
    timeout: float = 120.0
    http_client: Any = Field(default=None, exclude=True)
    http_async_client: Any = Field(default=None, exclude=True)
    _client: Any = PrivateAttr()
    _async_client: Any = PrivateAttr()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        options = {"api_key": self.api_key.get_secret_value(), "base_url": self.base_url,
                   "max_retries": self.max_retries, "timeout": self.timeout}
        self._client, self._async_client = responses_clients(options, self.http_client, self.http_async_client)

    @property
    def _llm_type(self):
        return "focus-responses"

    @property
    def _identifying_params(self):
        return {"model": self.model_name, "provider": self.provider_contract.provider,
                "protocol": self.provider_contract.protocol}

    @property
    def lc_secrets(self):
        return {"api_key": "OPENAI_API_KEY"}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if isinstance(tool_choice, str) and tool_choice not in {"auto", "none", "required"}:
            tool_choice = {"type": "function", "name": tool_choice}
        return self.bind(tools=function_specs(tools), tool_choice=tool_choice, **kwargs)

    def with_structured_output(self, schema=None, *, method="json_schema", include_raw=False, strict=None, **kwargs):
        from focus.models.response_structured import structured_runnable

        return structured_runnable(self, schema, method, include_raw, strict, kwargs)

    def request_payload(self, messages, *, stop=None, **kwargs):
        if stop:
            raise ValueError("Responses 不支持 Chat stop 参数")
        options = {key: getattr(self, key) for key in ("temperature", "top_p", "max_tokens", "reasoning_effort", "reasoning", "text")}
        options.update(kwargs)
        tools = options.pop("tools", ())
        payload = ResponsesRequestProjector(self.provider_contract).build(messages, model=self.model_name, tools=tools, **options)
        estimate = estimate_responses_budget(payload)
        window = self.provider_contract.context_window
        if window is not None and estimate > window:
            raise ValueError(f"完整 Responses 请求超出上下文窗口: estimate={estimate}, limit={window}")
        return payload

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        request = self.request_payload(messages, stop=stop, **kwargs)
        raw = self._client.responses.create(**request).model_dump(mode="json")
        raw["requested_model"] = self.model_name
        raw["request_source_manifest"] = response_source_manifest(request, messages, self.provider_contract)
        message = decode_response(raw, self.provider_contract, request["tools"])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        request = self.request_payload(messages, stop=stop, **kwargs)
        raw = (await self._async_client.responses.create(**request)).model_dump(mode="json")
        raw["requested_model"] = self.model_name
        raw["request_source_manifest"] = response_source_manifest(request, messages, self.provider_contract)
        message = decode_response(raw, self.provider_contract, request["tools"])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        request = self.request_payload(messages, stop=stop, stream=True, **kwargs)
        bridge = ResponseChunkBridge(self.provider_contract, request["tools"], model_name=self.model_name,
                                     source_manifest=response_source_manifest(request, messages, self.provider_contract))
        try:
            with self._client.responses.create(**request) as stream:
                for event in stream:
                    for message in bridge.accept(event.model_dump(mode="json")):
                        chunk = ChatGenerationChunk(message=message)
                        if run_manager:
                            run_manager.on_llm_new_token(response_text(message), chunk=chunk)
                        yield chunk
                for message in bridge.finish():
                    yield ChatGenerationChunk(message=message)
        except ResponseAttemptError:
            raise
        except Exception as exc:
            raise ResponseAttemptError("disconnected", bridge.audit(), type(exc).__name__) from exc

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        request = self.request_payload(messages, stop=stop, stream=True, **kwargs)
        bridge = ResponseChunkBridge(self.provider_contract, request["tools"], model_name=self.model_name,
                                     source_manifest=response_source_manifest(request, messages, self.provider_contract))
        try:
            async with await self._async_client.responses.create(**request) as stream:
                async for event in stream:
                    for message in bridge.accept(event.model_dump(mode="json")):
                        chunk = ChatGenerationChunk(message=message)
                        if run_manager:
                            await run_manager.on_llm_new_token(response_text(message), chunk=chunk)
                        yield chunk
                for message in bridge.finish():
                    yield ChatGenerationChunk(message=message)
        except asyncio.CancelledError as exc:
            exc.audit = bridge.audit()
            raise
        except ResponseAttemptError:
            raise
        except Exception as exc:
            raise ResponseAttemptError("disconnected", bridge.audit(), type(exc).__name__) from exc
