"""保存后用生效配置验证普通调用、真实策展与自动语义索引输出合同，结果只含脱敏诊断。

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
    labels = {"chat": "普通调用", "curation": "策展输出", "semantic_index": "自动索引"}
    for capability in labels:
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
            elif capability == "curation":
                name = config.resolve_curation_model_name()
                source = CurationSourceSnapshot(
                    source_checkpoint_id="settings-probe", projection_hash="0" * 64,
                    messages=[{"source_message_id": "probe", "role": "human", "content": "请保留这条测试消息。"}],
                )
                payload = build_curation_input(source, 0, {}, [])
                result = await asyncio.wait_for(CurationEngine(config).curate(name, payload), timeout=30)
                compiled = compile_curated_context(result.plan, source)
                if compiled.outcome != "replace" or not compiled.authored_messages:
                    raise ValueError("策展未返回有效上下文，请检查策展输出方式及模型能力")
            else:
                name = config.resolve_curation_model_name()
                await asyncio.wait_for(_verify_semantic_index(config), timeout=30)
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
    reason = "普通调用、策展输出和自动索引验证通过" if ok else "；".join(
        f"{labels[check['capability']]}：{check['reason']}"
        for check in checks if not check["ok"]
    )
    return {"ok": ok, "reason": reason, "checks": checks}


async def _verify_semantic_index(config: AppConfig) -> None:
    """走生产 projector 的 schema、prompt 及缺省模型选择，不写入索引。"""
    from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
        RevisionSemanticIndexer, SupervisedSegmentSemanticProjector,
    )
    from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
    from backend.app.desktop.context_evolution import ContextRevisionRef

    source = ContextRevisionRef(
        context_id="settings-probe", revision_id="settings-probe", generation=1,
        execution_thread_id="settings-probe", checkpoint_id="settings-probe", payload_mode="checkpoint",
    )
    index = RevisionSemanticIndexer().index(
        source=source, source_content_hash="0" * 64, context_role="requirements",
        active_objective="验证模型配置",
        raw_messages=({"id": "probe", "role": "human", "content": "请保留这条测试消息。"},),
    )
    model = RoleBoundStructuredModel(config, "semantic_index_projector", max_attempts=1)
    try:
        units = await SupervisedSegmentSemanticProjector(model).project(index)
    except ValueError as exc:
        raise ValueError("自动索引输出不符合合同，请检查策展输出方式、模型名称与协议后重新保存验证") from exc
    if not units:
        raise ValueError("自动索引未返回有效语义单元，请检查策展输出方式及模型能力")
