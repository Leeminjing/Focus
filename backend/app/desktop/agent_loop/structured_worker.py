r"""本文件对外提供 StructuredWorkerModel 及其 provider 单请求窗口、输出预留和真实用量读取接口。

输入为 AppConfig、可选模型名、Pydantic 输出 schema、角色专属 authority prompt 与冻结 JSON payload；输出为严格 schema 校验的
结构化模型结果、请求窗口、最近一次调用及累计 ModelUsage，并标明 provider 是否报告真实 Token 与可用模型元数据。具体工作流为按 curation 配置读取窗口与输出限制，创建无工具 chat model，使用 prompt_json 或 provider
structured output 调用，统一剥离 fenced JSON、校验 extra-forbid schema，并以独立 callback 记录每次调用 usage 或显式标记缺失；本模块不决定业务阶段或状态。
示例：`result = await StructuredWorkerModel(config).invoke(MySchema, system, payload)`。
"""

from __future__ import annotations

import json
from typing import Any

from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.context.requests import frozen_request_messages
from focus.history import content_hash
from focus.runtime.runs.usage import ModelUsage, callback_usage
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage


class StructuredWorkerModel:
    def __init__(self, app_config: AppConfig, model_name: str | None = None) -> None:
        self._app_config = app_config
        self._model_name = model_name
        self.usage = ModelUsage()
        self.last_usage = ModelUsage()
        self.last_usage_reported = False
        self.last_model_metadata = {}

    @property
    def context_window_tokens(self) -> int | None:
        return self._model_config().context_window

    @property
    def max_output_tokens(self) -> int:
        return self._model_config().curation_max_output_tokens

    def _model_config(self):
        return self._app_config.get_model(
            self._model_name or self._app_config.resolve_default_model_name()
        )

    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        config = self._model_config()
        self.last_model_metadata = {"configured_model": config.model}
        model = create_chat_model(
            name=config.name,
            app_config=self._app_config,
            max_tokens=config.curation_max_output_tokens,
        )
        document = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        json_schema = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = frozen_request_messages(system, f"<worker_input>{document}</worker_input>\nJSON Schema: {json_schema}",
                                           scope="round", source_refs=({"payload_hash": content_hash(payload)},))
        callback = UsageMetadataCallbackHandler()
        try:
            invoke_config = {"callbacks": [callback]}
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
                return schema.model_validate(json.loads(candidate))
            response = await model.with_structured_output(
                schema,
                method=config.curation_output_method,
                include_raw=True,
            ).ainvoke(messages, config=invoke_config)
            if isinstance(response, dict) and "raw" in response:
                metadata = getattr(response["raw"], "response_metadata", {}) or {}
                self.last_model_metadata.update({key: metadata[key] for key in
                    ("provider", "protocol", "model_name", "request_source_manifest", "usage") if key in metadata})
                if response.get("parsing_error") is not None:
                    raise response["parsing_error"]
                response = response["parsed"]
            return (
                response
                if isinstance(response, schema)
                else schema.model_validate(response)
            )
        finally:
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

    @staticmethod
    def _json_text(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip()
        return candidate
