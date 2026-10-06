"""本文件对外提供 ArtifactContentProjection 的可信完整文本读面。

输入为同源校验合格的 Artifact、当前 revision、只读 session 及秘密值；输出为绑定原来源/hash 的完整脱敏文本或安全拒绝。
具体工作流为复核 Run 的工作区和成功文件工具审计，在原边界内读取当前字节并核对摘要、大小及完整 UTF-8。
read_file 返回必须逐字相同；write_file 的已记录不可变摘要及实际返回路径绑定写入证明。正文不赋值领域 ORM 或旧冻结输入。
示例：content = await ArtifactContentProjection().project(session, source, revision, secrets=keys)。
本模块不提供模型工具、不认定检查通过；模型请求容量及最终授权由既有调用方复检。
"""

import asyncio
from hashlib import sha256
from pathlib import Path

from langchain_core.messages import ToolMessage

from backend.app.desktop.agent_loop.completion_sources import CompletionSourceRejection
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.models import DesktopRun, DesktopWorkspace, ToolExecutionAttempt
from backend.app.desktop.secret_redaction import redact_text
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.history import deserialize_history_message


class ArtifactContentProjection:
    contract = 'trusted-artifact-content-v1'

    async def project(self, session, source, revision, *, secrets=()):
        proof = source.payload.get('execution_proof') or {}
        run = await session.get(DesktopRun, source.run_id) if source.run_id else None
        slot_id = (run.workspace_anchor or {}).get('slot_id') if run else None
        slot = await session.get(WorkspaceSlot, slot_id) if slot_id else None
        loop = await session.get(AgentLoop, source.loop_id)
        workspace = await session.get(DesktopWorkspace, loop.workspace_id) if loop else None
        attempt = await session.get(ToolExecutionAttempt, proof.get('attempt_id')) if proof.get('attempt_id') else None
        if (run is None or slot is None or workspace is None or slot.workspace_id != workspace.workspace_id
            or attempt is None or proof.get('bound') is not True
            or proof.get('run_id') != run.run_id or attempt.run_id != run.run_id
            or attempt.status != 'completed' or attempt.call_id != proof.get('call_id')
            or attempt.call_hash != proof.get('call_hash') or proof.get('tool_name') not in {'read_file', 'write_file'}):
            self._reject(source, revision, 'source_unsupported')
        try:
            message = deserialize_history_message(attempt.result) if attempt.result else None
        except (KeyError, TypeError, ValueError):
            self._reject(source, revision, 'source_unsupported')
        if (not isinstance(message, ToolMessage) or message.status != 'success' or message.name != proof['tool_name']
            or message.tool_call_id != attempt.call_id or not isinstance(message.content, str)):
            self._reject(source, revision, 'source_unsupported')
        body = await asyncio.to_thread(self._read, source, revision, slot.root_path, proof, message.content, workspace.path)
        redacted = redact_text(body, secrets)
        return {'contract': self.contract, 'source_result_key': source.result_key, 'sha256': source.payload['sha256'],
            'encoding': 'utf-8', 'complete': True, 'text': redacted, 'redacted': redacted != body}

    @classmethod
    def _read(cls, source, revision, workspace, proof, output, expected_workspace):
        try:
            root = Path(workspace).resolve()
            path = (root / str(source.payload.get('path') or '')).resolve()
            inside = (path.is_relative_to(root) and path.is_file() and root == Path(expected_workspace).resolve()
                and Path(str(proof.get('workspace') or '')).resolve() == root)
        except (OSError, RuntimeError, ValueError):
            cls._reject(source, revision, 'source_unsettled')
        if not inside:
            cls._reject(source, revision, 'source_unsupported')
        try:
            raw = path.read_bytes()
        except OSError:
            cls._reject(source, revision, 'source_unsettled')
        if (source.payload.get('workspace_revision') != revision or source.payload.get('sha256') != sha256(raw).hexdigest()
            or source.payload.get('size_bytes') != len(raw)):
            cls._reject(source, revision, 'source_stale')
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            cls._reject(source, revision, 'source_unsupported')
        if '\x00' in body:
            cls._reject(source, revision, 'source_unsupported')
        expected = body if proof['tool_name'] == 'read_file' else f'已写入真实宿主机路径: {path}'
        if output != expected:
            cls._reject(source, revision, 'source_unsupported')
        return body

    @staticmethod
    def _reject(source, revision, code):
        raise CompletionSourceRejection(code, '产物正文无法提供可信完整文本', source.result_key, revision,
            source.payload.get('workspace_revision'))
