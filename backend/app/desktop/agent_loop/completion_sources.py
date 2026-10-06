"""本文件对外提供 CompletionSourceValidator 与安全的 CompletionSourceRejection。

输入为只读或最终权威事务、Loop/revision、声明检查与不可变来源身份；输出为合格来源或带 check/source/code/revision 的拒绝。
具体工作流为 validate 逐项复用 qualify，核对真实执行结算、命令证明/权限/工作区、产物调用与当前内容哈希。
同一 qualify 供冻结目录和模型候选使用；最终提交仍复检，不因候选通过获得写入权。错误不含正文、路径或凭据。
新版正文请求的候选与提交通过 require_artifact_content 复检完整可信正文，旧验证历史不补写。
示例：await validator.validate(session, checks, contract)；await validator.qualify(session, loop_id, revision, key, 'test')。
"""

from hashlib import sha256
from pathlib import Path

from sqlalchemy import or_, select

from backend.app.desktop.agent_loop.derivation_worker import StructuredResultValidationError
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopWorkspace, ToolExecutionAttempt
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class CompletionSourceRejection(StructuredResultValidationError):
    def __init__(self, code, message, source_id, expected, actual=None):
        self.expected_revision = expected
        self.actual_revision = actual
        super().__init__(code, f"{message}; source={source_id}; expected_revision={expected}; actual_revision={actual}",
                         unit_identity=source_id, violated_rule=code)


class CompletionSourceValidator:
    async def validate(self, session, checks, contract, *, frozen_sources=None, require_artifact_content=False):
        await self._workspace(session, contract.loop_id, contract.workspace_revision)
        frozen = {s["source_id"]: s["payload"] for s in frozen_sources} if frozen_sources is not None else None
        declared = {str(check["check_id"]): check for check in checks}
        for criterion in contract.criteria:
            if criterion.status != "satisfied":
                continue
            if not criterion.evidence:
                raise ValueError(f"完成检查 {criterion.check_id}: 必要检查缺少证据")
            for evidence in criterion.evidence:
                try:
                    if frozen is not None and evidence.source_id not in frozen:
                        self._reject("source_not_frozen", "来源不属于冻结目录", contract.loop_id, contract.workspace_revision)
                    source = await self.qualify(session, contract.loop_id, contract.workspace_revision, evidence.source_id,
                                       evidence.kind, user_verification=declared[criterion.check_id].get("user_verification", False),
                                       frozen_payload=frozen[evidence.source_id] if frozen is not None else None)
                    if require_artifact_content and source.kind == 'artifact':
                        from backend.app.desktop.agent_loop.artifact_content import ArtifactContentProjection

                        await ArtifactContentProjection().project(session, source, contract.workspace_revision)
                except CompletionSourceRejection as exc:
                    raise CompletionSourceRejection(exc.code, f"完成检查 {criterion.check_id}: {exc}",
                                                    evidence.source_id, contract.workspace_revision, exc.actual_revision) from exc

    async def qualify(self, session, loop_id, revision, source_id, kind, *, user_verification=False, frozen_payload=None):
        source = await session.scalar(select(DesktopDomainResult).where(
            DesktopDomainResult.loop_id == loop_id,
            or_(DesktopDomainResult.result_key == source_id, DesktopDomainResult.source_id == source_id),
        ).order_by(DesktopDomainResult.created_at.desc()).limit(1))
        if source is None:
            self._reject("source_missing", "必要证据不存在或无可信归属", source_id, revision)
        if frozen_payload is not None and (source.result_key != source_id or source.payload != frozen_payload):
            self._reject("source_identity_changed", "冻结来源内容或身份不一致", source_id, revision)
        workspace = await self._workspace(session, loop_id, revision)
        if kind == "user" and user_verification and source.kind == "user_revision" and source.payload.get("confirmed") is True:
            return source
        run = await session.get(DesktopRun, source.run_id) if source.run_id else None
        if run is None or run.loop_id != loop_id or run.round_id is None or run.settled_at is None:
            self._reject("source_unsettled", "证据执行未结算", source_id, revision)
        result = run.workspace_result or {}
        if result.get("revision") != revision:
            self._reject("source_stale", "工作区证据 stale 或未稳定", source_id, revision, result.get("revision"))
        if result.get("workspace_status") != "settled":
            self._reject("source_unsettled", "工作区证据 stale 或未稳定", source_id, revision, result.get("revision"))
        if kind in {"test", "tool", "fact"} and source.kind == "test":
            await self._test(session, source, run, workspace, revision)
        elif kind == "artifact" and source.kind == "artifact":
            await self._artifact(session, source, run, workspace, revision)
        elif kind == "workspace" and source.kind == "workspace":
            if source.payload.get("revision") != revision:
                self._reject("source_stale", "工作区来源 stale", source_id, revision, source.payload.get("revision"))
        else:
            self._reject("source_unsupported", "自述或证据类型不能证明通过", source_id, revision)
        return source

    async def _workspace(self, session, loop_id, revision):
        loop = await session.get(AgentLoop, loop_id)
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id,
            WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
        if slot is not None and slot.revision != revision:
            self._reject("workspace_changed", "当前工作区版本已变化", loop_id, revision, slot.revision)
        return await session.get(DesktopWorkspace, loop.workspace_id)

    async def _test(self, session, source, run, workspace, revision):
        proof = source.payload.get("execution_proof") or {}
        if (source.payload.get("status") != "verified" or proof.get("exit_code") != 0
            or proof.get("status") != "exited" or proof.get("bound") is not True
            or proof.get("run_id") != run.run_id or not proof.get("call_id")
            or "host_command" not in (run.equipment or {}).get("permissions", ())):
            self._reject("source_failed", "命令未执行、失败或缺少能力", source.result_key, revision)
        slot_id = (run.workspace_anchor or {}).get("slot_id")
        slot = await session.get(WorkspaceSlot, slot_id) if slot_id else None
        root = Path(slot.root_path if slot else workspace.path).resolve()
        if not proof.get("workspace") or Path(proof["workspace"]).resolve() != root:
            self._reject("source_wrong_workspace", "命令工作区来源不匹配", source.result_key, revision)
        actual = source.payload.get("workspace_revision", revision)
        if actual != revision:
            self._reject("source_stale", "测试来源 stale", source.result_key, revision, actual)

    async def _artifact(self, session, source, run, workspace, revision):
        root = Path(workspace.path).resolve()
        path = (root / str(source.payload.get("path") or "")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            self._reject("source_missing", "产物不存在或超出工作区", source.result_key, revision)
        proof = source.payload.get("execution_proof")
        if proof is not None:
            attempt = await session.get(ToolExecutionAttempt, proof.get("attempt_id"))
            try:
                current_hash = sha256(path.read_bytes()).hexdigest()
            except OSError:
                self._reject("source_unsettled", "产物当前内容无法核对", source.result_key, revision)
            if (attempt is None or attempt.status != "completed" or attempt.run_id != run.run_id
                or attempt.call_id != proof.get("call_id") or attempt.call_hash != proof.get("call_hash")
                or source.payload.get("workspace_revision") != revision
                or source.payload.get("sha256") != current_hash):
                self._reject("source_stale", "产物调用或内容已过期", source.result_key, revision, source.payload.get("workspace_revision"))

    @staticmethod
    def _reject(code, message, source_id, expected, actual=None):
        raise CompletionSourceRejection(code, message, source_id, expected, actual)
