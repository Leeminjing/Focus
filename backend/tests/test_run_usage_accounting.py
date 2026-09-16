r"""本文件验证统一模型用量的流式去重与 callback 聚合。

输入为同一模型调用的累计 AIMessageChunk 以及多个 provider 的 UsageMetadata；输出为调用数、输入、
输出与 cache-read token 的标准 ModelUsage。具体工作流为流式路径按调用 identity 取最大累计快照，
非流式路径按 callback 中的模型聚合。示例：`pytest test_run_usage_accounting.py`。
"""

from langchain_core.messages import AIMessageChunk

from focus.runtime.runs.usage import StreamUsageAccumulator, callback_usage


def test_stream_usage_uses_latest_cumulative_snapshot_per_model_call() -> None:
    usage = StreamUsageAccumulator()
    metadata = {"langgraph_step": 2, "langgraph_node": "model"}
    usage.observe("messages", (AIMessageChunk(id="call-1", content="a", usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}), metadata))
    usage.observe("messages", (AIMessageChunk(id="call-1", content="b", usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "input_token_details": {"cache_read": 6}}), metadata))
    usage.observe("messages", (AIMessageChunk(id="call-2", content="c", usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}), {"langgraph_step": 3, "langgraph_node": "model"}))

    total = usage.total()
    assert total.model_calls == 2
    assert total.input_tokens == 17
    assert total.output_tokens == 6
    assert total.cache_read_input_tokens == 6


def test_callback_usage_aggregates_each_provider_bucket() -> None:
    callback = type("Callback", (), {"usage_metadata": {
        "model-a": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        "model-b": {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12, "input_token_details": {"cache_read": 4}},
    }})()

    total = callback_usage(callback)
    assert total.model_calls == 2
    assert total.input_tokens == 14
    assert total.output_tokens == 5
    assert total.cache_read_input_tokens == 4
