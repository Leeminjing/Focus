"""本文件对外提供四请求受控 Responses smoke 命令。

输入为现有模型目录中的显式模型名、凭据环境变量和报告路径；输出为不含凭据或完整载荷的能力验证记录。
具体工作流为强制 function call、手工 replay、JSON schema 及公开 reasoning 流；每次最多 512 输出 tokens、零自动重试。
示例：python backend/scripts/responses_smoke.py --model deepseek-v4-flash --report smoke.json。
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "harness"))

from focus.config import get_app_config
from focus.models import create_chat_model
from focus.models.provider_contract import resolve_provider_contract
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel


@tool
def smoke_lookup(key: str) -> str:
    """Return fixed local smoke evidence for the given key."""
    return "ok"


class Verdict(BaseModel):
    ok: bool


async def smoke(name, only_reasoning=False):
    config = get_app_config("config.yaml")
    entry = config.get_model(name)
    explicit = entry.model_copy(update={"protocol": "responses", "provider": resolve_provider_contract(entry).provider})
    config = config.model_copy(update={"models": [explicit]})
    model = create_chat_model(name=name, app_config=config, max_tokens=512, reasoning_effort="high" if only_reasoning else "none", max_retries=0, timeout=20)
    report = {"provider": model.provider_contract.provider, "model": model.model_name, "max_output_tokens_per_request": 512}
    try:
        first = await model.bind_tools([smoke_lookup], tool_choice="auto" if only_reasoning else "smoke_lookup").ainvoke([
            SystemMessage(content="Call smoke_lookup with key x."), HumanMessage(content="Run the lookup.")])
        report["tool_call"] = "passed" if len(first.tool_calls) == 1 else "failed"
        call = first.tool_calls[0]
        second = await model.bind_tools([smoke_lookup]).ainvoke([HumanMessage(content="Run a lookup and reply ok."), first,
                                                              ToolMessage(content="ok", tool_call_id=call["id"])])
        report["manual_replay"] = "passed" if second.response_metadata.get("status") == "completed" else "failed"
        if only_reasoning:
            report["reasoning_replay"] = "passed" if any(item["type"] == "reasoning" for item in first.additional_kwargs["focus_response_items"]) else "unverified_no_reasoning_item"
            return report
        verdict = await model.with_structured_output(Verdict).ainvoke([SystemMessage(content="Return ok true."),
                                                                    HumanMessage(content="Return the verdict.")])
        report["structured"] = "passed" if verdict.ok else "failed"
        chunks = [chunk async for chunk in model.model_copy(update={"reasoning_effort": "high"}).astream([
            HumanMessage(content="What is 2+2? Reply briefly.")])]
        combined = sum(chunks[1:], chunks[0])
        report["reasoning_stream"] = "passed" if combined.response_metadata.get("status") == "completed" else "failed"
        report["reasoning_item"] = any(item["type"] == "reasoning" for item in combined.additional_kwargs["focus_response_items"])
        report["usage_reported"] = all(message.usage_metadata is not None for message in (first, second, combined))
    except BaseException as exc:
        report.update(failure_type=type(exc).__name__, http_status=getattr(exc, "status_code", None),
                      attempt_status=getattr(exc, "status", None))
    finally:
        await model._async_client.close()
        model._client.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--reasoning-replay-only", action="store_true")
    args = parser.parse_args()
    load_dotenv(".env")
    report = asyncio.run(smoke(args.model, args.reasoning_replay_only))
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))
