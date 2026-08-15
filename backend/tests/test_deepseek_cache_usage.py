"""验证 DeepSeek 的缓存 token 字段映射到 LangChain 标准 usage。"""

from langchain_core.messages import AIMessageChunk

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
