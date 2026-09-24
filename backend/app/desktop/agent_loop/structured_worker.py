r"""本文件对外提供 StructuredWorkerModel。

输入为 AppConfig、可选模型名、Pydantic 输出 schema、角色专属 authority prompt 与冻结 JSON payload；输出为严格 schema 校验的
结构化模型结果、最近一次调用及累计 ModelUsage。具体工作流为按 curation 配置创建无工具 chat model，使用 prompt_json 或 provider
structured output 调用，统一剥离 fenced JSON、校验 extra-forbid schema，并以独立 callback 记录每次调用 usage；本模块不决定业务阶段或状态。
示例：`result = await StructuredWorkerModel(config).invoke(MySchema, system, payload)`。
"""

from __future__ import annotations

import json
from typing import Any

from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.runtime.runs.usage import ModelUsage, callback_usage
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage


class StructuredWorkerModel:
    def __init__(self, app_config: AppConfig, model_name: str | None = None) -> None:
        self._app_config = app_config
        self._model_name = model_name
        self.usage = ModelUsage()
        self.last_usage = ModelUsage()

    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        config = self._app_config.get_model(
            self._model_name or self._app_config.resolve_default_model_name()
        )
        model = create_chat_model(
            name=config.name,
            app_config=self._app_config,
            max_tokens=config.curation_max_output_tokens,
        )
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        json_schema = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=f"<worker_input>{document}</worker_input>\nJSON Schema: {json_schema}"),
        ]
        callback = UsageMetadataCallbackHandler()
        try:
            invoke_config = {"callbacks": [callback]}
            if config.curation_output_method == "prompt_json":
                response = await model.ainvoke(messages, config=invoke_config)
                content = getattr(response, "content", response)
                text = content if isinstance(content, str) else "".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict)
                )
                candidate = self._json_text(text)
                return schema.model_validate(json.loads(candidate))
            response = await model.with_structured_output(
                schema,
                method=config.curation_output_method,
            ).ainvoke(messages, config=invoke_config)
            return response if isinstance(response, schema) else schema.model_validate(response)
        finally:
            measured = callback_usage(callback)
            self.last_usage = measured if measured.model_calls else ModelUsage(model_calls=1)
            self.usage += self.last_usage

    @staticmethod
    def _json_text(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip()
        return candidate
