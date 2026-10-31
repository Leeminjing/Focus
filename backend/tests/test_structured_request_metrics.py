"""结构化请求的首输出、未知用量、合同失败及无正文报告回归。"""

from types import SimpleNamespace

import pytest

from focus.models import structured_metrics as module
from focus.runtime.runs.usage import ModelUsage
from backend.scripts.desktop_performance_report import summarize


@pytest.mark.parametrize("kind", ["text", "reasoning", "tool"])
def test_first_token_ignores_empty_chunks_and_measures_generation(monkeypatch, kind):
    now, events = [1.0], []
    monkeypatch.setattr(module, "perf_counter", lambda: now[0])
    monkeypatch.setattr(module, "emit", lambda event, **fields: events.append({"event": event, **fields}))
    metrics = module.StructuredRequestMetrics()
    now[0] = 1.1
    metrics.on_llm_new_token("")
    assert metrics.first_token is None
    now[0] = 1.2
    message = SimpleNamespace(tool_call_chunks=[{"args": "private tool"}] if kind == "tool" else [],
        additional_kwargs={"reasoning_content": "private reasoning"} if kind == "reasoning" else {})
    metrics.on_llm_new_token("private content" if kind == "text" else "", chunk=SimpleNamespace(message=message))
    now[0] = 1.5
    metrics.on_llm_new_token("another token")
    metrics.on_llm_end(None)
    now[0] = 2.0  # Parsing time is outside output generation.
    record = metrics.finish(category="patrol", model="fast", schema=SimpleNamespace(__name__="Decision"),
        estimated_input_tokens=400, usage=ModelUsage(model_calls=1, input_tokens=350, output_tokens=20),
        usage_reported=True, contract_valid=True)
    assert record["ttft_ms"] == pytest.approx(200)
    assert record["output_duration_ms"] == pytest.approx(300)
    assert record["duration_ms"] == pytest.approx(500)
    assert "private" not in str(events)
    report = summarize(events)["structured_requests"][0]
    assert report["requests"] == report["usage_reported_requests"] == 1
    assert report["schema_success_rate"] == 1
    assert report["input_tokens"]["p50"] == 350


def test_unreported_tokens_and_nonstream_ttft_stay_unknown(monkeypatch):
    events = []
    monkeypatch.setattr(module, "emit", lambda event, **fields: events.append({"event": event, **fields}))
    metrics = module.StructuredRequestMetrics()
    metrics.on_llm_error(RuntimeError("private error"))
    record = metrics.finish(category="curator", model="deep", schema=SimpleNamespace(__name__="Advice"),
        estimated_input_tokens=100, usage=ModelUsage(model_calls=1), usage_reported=False, contract_valid=False)
    assert all(record[field] is None for field in ("ttft_ms", "output_duration_ms", "input_tokens", "output_tokens"))
    report = summarize(events)["structured_requests"][0]
    assert report["schema_success_rate"] == 0 and report["usage_reported_requests"] == 0
    assert report["input_tokens"]["samples"] == 0 and report["ttft_ms"]["p50"] is None
    assert "private" not in str(events)
