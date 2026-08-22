"""验证 DeepSeek 缓存 usage 与 reasoning_content 的薄适配。"""

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from focus.models.deepseek import DeepSeekChatOpenAI


def test_deepseek_stream_usage_maps_cache_hit_tokens():
    model = DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test",
        base_url="https://api.deepseek.com",
    )
    generation = model._convert_chunk_to_generation_chunk(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_cache_hit_tokens": 35,
                "prompt_cache_miss_tokens": 65,
            },
        },
        AIMessageChunk,
        None,
    )

    assert model.stream_usage is True
    assert generation.message.usage_metadata["input_tokens"] == 100
    assert generation.message.usage_metadata["input_token_details"]["cache_read"] == 35


def test_deepseek_stream_and_non_stream_preserve_reasoning_content():
    model = DeepSeekChatOpenAI(model="deepseek-chat", api_key="test", base_url="https://api.deepseek.com")
    stream = model._convert_chunk_to_generation_chunk(
        {
            "choices": [{"delta": {"role": "assistant", "content": None, "reasoning_content": "先分析"}, "finish_reason": None}],
        },
        AIMessageChunk,
        None,
    )
    assert stream.message.additional_kwargs["reasoning_content"] == "先分析"

    result = model._create_chat_result({
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "答案", "reasoning_content": "完整思考"}, "finish_reason": "stop"}],
    })
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "完整思考"


def test_deepseek_only_replays_reasoning_for_tool_call_messages():
    model = DeepSeekChatOpenAI(model="deepseek-chat", api_key="test", base_url="https://api.deepseek.com")
    tool_reasoning = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "需要读文件"},
        tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "a.md"}}],
    )
    final_reasoning = AIMessage(content="完成", additional_kwargs={"reasoning_content": "最终思考"})
    payload = model._get_request_payload([HumanMessage(content="开始"), tool_reasoning, final_reasoning])
    assert payload["messages"][1]["reasoning_content"] == "需要读文件"
    assert "reasoning_content" not in payload["messages"][2]
