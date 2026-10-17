"""保存后用生效配置验证普通调用和真实策展输出合同，结果只含脱敏诊断。

输入为独立配置快照，输出为逐项能力检查；所有调用有时间上限，不启动任务或写入对话。
"""

from __future__ import annotations

import asyncio

from focus.config.app_config import AppConfig
from backend.app.desktop.model_settings import _describe_failure
from backend.app.desktop.secret_redaction import redact_text


async def verify_model_setup(config: AppConfig) -> dict:
    from focus.models import create_chat_model
    from backend.app.desktop.context_curator.contract import (
        CurationSourceSnapshot, build_curation_input, compile_curated_context,
    )
    from backend.app.desktop.context_curator.engine import CurationEngine, CurationEngineError

    secrets = [model.api_key for model in config.models]
    checks = []
    for capability in ("chat", "curation"):
        name = None
        try:
            if capability == "chat":
                name = config.resolve_default_model_name()
                model = create_chat_model(
                    name=name, app_config=config, max_tokens=1024, max_retries=0, timeout=25,
                )
                response = await asyncio.wait_for(model.ainvoke("Reply with the word OK."), timeout=30)
                if not CurationEngine._content_text(getattr(response, "content", "")).strip():
                    raise ValueError("普通调用未返回文本，请检查模型名称、协议和输出上限")
            else:
                name = next(model.name for model in config.models if model.curation_default)
                source = CurationSourceSnapshot(
                    source_checkpoint_id="settings-probe", projection_hash="0" * 64,
                    messages=[{"source_message_id": "probe", "role": "human", "content": "请保留这条测试消息。"}],
                )
                payload = build_curation_input(source, 0, {}, [])
                result = await asyncio.wait_for(CurationEngine(config).curate(name, payload), timeout=30)
                compiled = compile_curated_context(result.plan, source)
                if compiled.outcome != "replace" or not compiled.authored_messages:
                    raise ValueError("策展未返回有效上下文，请检查策展输出方式及模型能力")
            checks.append({"capability": capability, "model": name, "ok": True})
        except Exception as exc:
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                reason = "请求超时，请检查网络、代理和端点地址后重新保存验证"
            elif isinstance(exc, CurationEngineError) and exc.kind in {"parse", "capability"}:
                reason = "策展输出不符合合同，请检查策展输出方式、模型名称与协议后重新保存验证"
            elif isinstance(exc, (ValueError, StopIteration, KeyError)):
                reason = str(exc) or "请指定可用的默认模型和策展默认模型"
            else:
                cause = exc.__cause__ if isinstance(exc, CurationEngineError) else exc
                reason = _describe_failure(cause or exc, secrets)
            checks.append({
                "capability": capability, "model": name, "ok": False,
                "reason": redact_text(reason, secrets),
            })
    ok = all(check["ok"] for check in checks)
    reason = "普通调用和策展输出验证通过" if ok else "；".join(
        f"{'普通调用' if check['capability'] == 'chat' else '策展输出'}：{check['reason']}"
        for check in checks if not check["ok"]
    )
    return {"ok": ok, "reason": reason, "checks": checks}
