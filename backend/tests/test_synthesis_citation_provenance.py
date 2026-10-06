"""本文件对外提供独立 Claim Verifier 的冻结来源保真回归。
输入为同正文不同消息角色、完整工具来源及类型化 Mission；输出为实际模型请求保留来源元数据、正文和身份的断言。
具体工作流为经过生产 RoleBoundStructuredModel 与 synthesis 服务捕获 Provider 请求，按声明引用身份读取共享目录，核对精确冻结对象，没有额外证据或角色升级。
示例：pytest backend/tests/test_synthesis_citation_provenance.py -q；远端响应可控，仅验证请求合同，不宣称真实模型已支持或 Context 已发布。
"""

import asyncio

import pytest

from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle
from backend.app.desktop.context_curation import SourceMessageEvidence, evidence_ref_key
from backend.tests.test_synthesis_claim_feedback import _service


@pytest.mark.parametrize("role", ["human", "ai", "system", "tool"])
def test_verifier_preserves_exact_message_provenance(monkeypatch, role):
    async def run():
        service, spec, original, synthesis, verifier, calls = _service(monkeypatch)
        source = original.evidence.sources[0]
        message = source.messages[0].model_copy(update={"role": role})
        messages = (message,)
        frontier = original.evidence.evidence_frontier
        if role == "tool":
            message = message.model_copy(update={"name": "read_file", "tool_call_id": "read-state", "status": "success"})
            caller = SourceMessageEvidence(ref=message.ref.model_copy(update={"message_id": "read-call"}), role="ai",
                tool_calls=({"id": "read-state", "name": "read_file", "args": {"path": "fixture/state.txt"}},))
            messages = (caller, message)
            frontier = (caller.ref, message.ref)
        evidence = original.evidence.model_copy(update={"sources": (source.model_copy(update={"messages": messages}),), "evidence_frontier": frontier})
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=original.items)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        request = calls[-1][1]
        identity = request["claims"][0]["citations"][0]
        citation, = (item for item in request["evidence"] if item["identity"] == identity)
        assert citation["source"] == message.model_dump(mode="json", exclude={"content"})
        assert citation["identity"] == list(evidence_ref_key(message.ref))
        assert citation["content"] == message.content
        assert [name for name, _ in calls] == ["synthesis", "verifier"]
        assert synthesis.last_usage.model_calls == verifier.last_usage.model_calls == 1
    asyncio.run(run())


def test_verifier_preserves_typed_mission_section_without_adding_uncited_sources(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, synthesis_error="mission")
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        request = calls[-1][1]
        citations = request["claims"][0]["citations"]
        assert len(citations) == 1
        citation, = (item for item in request["evidence"] if item["identity"] == citations[0])
        ref = bundle.items[0].ref
        assert citation["source"] == {"ref": ref.model_dump(mode="json")}
        assert citation["content"] == bundle.evidence.structured_item(ref).content
        assert citation["identity"] == list(evidence_ref_key(ref))
    asyncio.run(run())
