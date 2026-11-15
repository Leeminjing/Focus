"""结构化请求的完整序列化输入估算及无正文的流式耗时统计。"""

from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableBinding, RunnableParallel
from openai.lib._parsing import type_to_response_format_param

from focus.messages.request_budget import estimate_request_budget, estimate_responses_budget
from focus.models.responses import FocusResponsesChatModel
from focus.models.telemetry import emit, model_context


def structured_input_tokens(model, messages, schema, method):
    options = {}
    if method != "prompt_json":
        binding = model.with_structured_output(schema, method=method, include_raw=True).first
        if isinstance(binding, RunnableParallel):
            binding = binding.steps__.get("raw")
        if not isinstance(binding, RunnableBinding) or binding.bound is not model:
            raise ValueError("structured_request_budget_unsupported_binding")
        options = {key: value for key, value in binding.kwargs.items() if not key.startswith("ls_")}
    if isinstance(model, FocusResponsesChatModel):
        return estimate_responses_budget(model.request_payload(messages, validate_window=False, **options))
    request = model._get_request_payload(messages, **options)
    if "response_format" in request:
        request["response_format"] = type_to_response_format_param(request["response_format"])
    return estimate_request_budget(request.pop("messages"), format_spec=request)


class StructuredRequestMetrics(BaseCallbackHandler):
    run_inline = True

    def __init__(self):
        self.started = perf_counter()
        self.first_token = None
        self.ended = None

    def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        message = getattr(chunk, "message", None)
        if self.first_token is None and (token or getattr(message, "tool_call_chunks", None)
                or getattr(message, "additional_kwargs", {}).get("reasoning_content")):
            self.first_token = perf_counter()

    def on_llm_end(self, response, **kwargs):
        self.ended = perf_counter()

    def on_llm_error(self, error, **kwargs):
        self.ended = perf_counter()

    def finish(self, *, category, model, schema, estimated_input_tokens, usage, usage_reported, contract_valid):
        end = self.ended or perf_counter()
        fields = {
            "estimated_input_tokens": estimated_input_tokens,
            "input_tokens": usage.input_tokens if usage_reported else None,
            "output_tokens": usage.output_tokens if usage_reported else None,
            "usage_reported": usage_reported,
            "ttft_ms": (self.first_token - self.started) * 1000 if self.first_token is not None else None,
            "output_duration_ms": (end - self.first_token) * 1000 if self.first_token is not None else None,
            "duration_ms": (end - self.started) * 1000,
            "contract_valid": contract_valid,
            "validation_scope": "schema",
        }
        context = {**(model_context.get() or {}), "category": category, "model": model, "schema": schema.__name__}
        emit("structured_request", **context, **fields)
        return fields
