r"""本文件对外提供 StructuredWorkerModel 及其 provider 单请求窗口、输出预留和真实用量读取接口。

输入为 AppConfig、可选模型名（未指定时使用策展默认）、Pydantic 输出 schema、角色专属 authority prompt 与冻结 JSON payload；输出为严格 schema 校验的
结构化模型结果、请求窗口、最近一次调用及累计 ModelUsage，并标明 provider 是否报告真实 Token 与可用模型元数据。具体工作流为按 curation 配置读取窗口与输出限制，创建无工具 chat model，使用 prompt_json 或 provider
structured output 调用，统一剥离 fenced JSON、校验 extra-forbid schema，并以独立 callback 记录每次调用 usage 或显式标记缺失；本模块不决定业务阶段或状态。
request_messages 提供冻结消息；estimate_input_tokens 复用真实 Runnable 的绑定参数和 adapter/SDK 序列化，
完整计入消息、text.format、response_format 与工具合同，供外部窗口准入及消费预留；预览不发请求、不产生用量。
request_model_config 提供该请求的模型配置。Responses 复用完整请求预算，legacy Chat 复用 SDK 严格 schema 转换及既有预算估算。
模型、schema 绑定及用量 callback 逐请求独立；Responses 的底层连接由 AppConfig 绑定的应用级会话复用。
示例：`result = await StructuredWorkerModel(config).invoke(MySchema, system, payload)`。
"""

from __future__ import annotations

import json
from typing import Any

from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.models.lifecycle import model_client_scope
from focus.context.requests import frozen_request_messages
from focus.history import content_hash
from focus.runtime.runs.usage import ModelUsage, callback_usage
from focus.models.structured_metrics import StructuredRequestMetrics, structured_input_tokens
from focus.models.telemetry import model_context
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage


class StructuredWorkerModel:
    def __init__(self, app_config: AppConfig, model_name: str | None = None, *, max_output_tokens: int | None = None) -> None:
        self._app_config = app_config
        self._model_name = model_name
        self._max_output_tokens = max_output_tokens
        self.usage = ModelUsage()
        self.last_usage = ModelUsage()
        self.last_usage_reported = False
        self.last_model_metadata = {}
        self._usage_receipts = None

    def bind_usage_receipts(self, receipts) -> None:
        self._usage_receipts = receipts

    @property
    def usage_managed(self) -> bool:
        return self._usage_receipts is not None

    @property
    def context_window_tokens(self) -> int | None:
        return self._model_config().context_window

    @property
    def max_output_tokens(self) -> int:
        return self._model_config().curation_max_output_tokens

    @property
    def small_evidence_max_bytes(self) -> int:
        return self._model_config().curation_small_evidence_max_bytes

    def _model_config(self):
        config = self._app_config.get_model(
            self._model_name or self._app_config.resolve_curation_model_name()
        )
        return config if self._max_output_tokens is None else config.model_copy(
            update={"curation_max_output_tokens": self._max_output_tokens}
        )

    def _chat_model(self):
        config = self._model_config()
        return create_chat_model(name=config.name, app_config=self._app_config,
                                 max_tokens=self.max_output_tokens,
                                 streaming=getattr(config, "curation_streaming", False))

    @staticmethod
    def _structured_request(model, schema, method):
        return model.with_structured_output(schema, method=method, include_raw=True)

    def estimate_input_tokens(self, schema, system, payload) -> int:
        model = self._chat_model()
        method = self._model_config().curation_output_method
        messages = self.request_messages(schema, system, payload, method=method)
        return structured_input_tokens(model, messages, schema, method)

    @model_client_scope()
    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        config = self._model_config()
        self.last_model_metadata = {"configured_model": config.model}
        model = self._chat_model()
        messages = self.request_messages(schema, system, payload, method=config.curation_output_method)
        estimated = structured_input_tokens(model, messages, schema, config.curation_output_method) if (
            hasattr(model, "request_payload") or hasattr(model, "_get_request_payload")
        ) else None
        callback = UsageMetadataCallbackHandler()
        receipt = None
        if self._usage_receipts is not None:
            if estimated is None:
                raise ValueError("structured_request_budget_unsupported_model")
            receipt = await self._usage_receipts.reserve(estimated, config.curation_max_output_tokens)
        metrics = StructuredRequestMetrics()
        contract_valid = False
        try:
            invoke_config = {"callbacks": [callback, metrics]}
            if config.curation_output_method == "prompt_json":
                response = await model.ainvoke(messages, config=invoke_config)
                metadata = getattr(response, "response_metadata", {}) or {}
                self.last_model_metadata.update(
                    {
                        key: metadata[key]
                        for key in ("model", "model_name", "system_fingerprint", "provider", "protocol", "request_source_manifest")
                        if key in metadata
                    }
                )
                content = getattr(response, "content", response)
                text = (
                    content
                    if isinstance(content, str)
                    else "".join(
                        str(item.get("text") or "")
                        for item in content
                        if isinstance(item, dict)
                    )
                )
                candidate = self._json_text(text)
                parsed = schema.model_validate(json.loads(candidate))
                contract_valid = True
                return parsed
            response = await self._structured_request(model, schema, config.curation_output_method).ainvoke(messages, config=invoke_config)
            if isinstance(response, dict) and "raw" in response:
                metadata = getattr(response["raw"], "response_metadata", {}) or {}
                self.last_model_metadata.update({key: metadata[key] for key in
                    ("provider", "protocol", "model_name", "request_source_manifest", "usage") if key in metadata})
                if response.get("parsing_error") is not None:
                    raise response["parsing_error"]
                response = response["parsed"]
            parsed = (
                response
                if isinstance(response, schema)
                else schema.model_validate(response)
            )
            contract_valid = True
            return parsed
        finally:
            self.last_model_metadata["usage_reported"] = bool(getattr(callback, "usage_metadata", None))
            self.last_model_metadata["usage_model_ids"] = tuple(
                (getattr(callback, "usage_metadata", None) or {}).keys()
            )
            measured = callback_usage(callback)
            reports = tuple((getattr(callback, "usage_metadata", None) or {}).values())
            self.last_usage_reported = bool(reports) and all(
                "input_tokens" in item and "output_tokens" in item for item in reports
            )
            self.last_usage = (
                measured if measured.model_calls else ModelUsage(model_calls=1)
            )
            self.usage += self.last_usage
            self.last_model_metadata.update(metrics.finish(
                category=(model_context.get() or {}).get("category", "curator"), model=config.model, schema=schema,
                estimated_input_tokens=estimated, usage=self.last_usage,
                usage_reported=self.last_usage_reported, contract_valid=contract_valid))
            if receipt is not None:
                await self._usage_receipts.settle(receipt, self.last_usage, self.last_usage_reported)

    @staticmethod
    def request_messages(schema, system, payload, *, method="prompt_json"):
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        json_schema = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        contract = f"\nJSON Schema: {json_schema}" if method == "prompt_json" else ""
        return frozen_request_messages(system, f"<worker_input>{document}</worker_input>{contract}",
                                       scope="round", source_refs=({"payload_hash": content_hash(payload)},))

    @property
    def request_model_config(self):
        return self._model_config()

    @staticmethod
    def _json_text(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip()
        return candidate
