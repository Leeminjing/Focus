"""本文件对外提供重复调用 synthesis 服务的 attempt 归属回归。
输入为生产 RoleBound 服务和受控响应；输出为失败调用不继承上一次独立核验消费的断言。
具体工作流为先完成一次正常编译，再制造作者冻结目录选择持续非法；示例：pytest backend/tests/test_synthesis_attempt_scope.py -q。
"""
import asyncio
import json
from langchain_core.messages import AIMessage
import backend.app.desktop.agent_loop.structured_worker as worker_module
from backend.tests.test_synthesis_claim_feedback import _service


def test_failed_author_cannot_inherit_previous_verifier_attempt(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch)
        first = await service.synthesize(None, spec, bundle)
        assert first.dossier is not None and len(first.attempt_records) == 2
        factory = worker_module.create_chat_model

        class FailingAuthorProvider:
            async def ainvoke(self, messages, config):
                response = await factory().ainvoke(messages, config)
                payload = json.loads(response.content)
                if "sections" in payload:
                    payload["sections"][0]["claims"][0]["citations"][0] = "unknown-current-ref"
                return AIMessage(content=json.dumps(payload))

        monkeypatch.setattr(worker_module, "create_chat_model", lambda **kwargs: FailingAuthorProvider())
        second = await service.synthesize(None, spec, bundle)
        assert second.dossier is None
        assert [role for role, _ in calls] == ["synthesis", "verifier", "synthesis", "synthesis"]
        assert len(second.attempt_records) == 2
        assert all(record["role"] == "dossier_synthesizer" for record in second.attempt_records)
    asyncio.run(run())

