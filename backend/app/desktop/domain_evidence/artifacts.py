"""本文件对外提供 ArtifactObservationRecorder，将实际文件工具结果保存为产物来源。

输入为已结算 Run、当前执行的文件调用与持久工具审计；输出为带相对路径、内容哈希和精确调用证明的
不可变 Artifact。具体工作流为核对同 Run 的成功执行及参数哈希，在其绑定工作区内读取实际文件，
用 Security 同源 canonical_target 还原实际执行参数，再核对账本哈希；只有内容与 read_file 返回值或 write_file 输入一致才记录；不从 Agent 自述、Shell 文本或目录猜测产物。
示例：await ArtifactObservationRecorder().record(session, run, calls)；不保存文件正文或密钥。
"""

import asyncio
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.models import ToolExecutionAttempt
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.history import content_hash, deserialize_history_message
from focus.security.paths import canonical_target


class ArtifactObservationRecorder:
    async def record(self, session, run, calls):
        if run.settled_at is None or (run.workspace_result or {}).get("workspace_status") != "settled":
            return
        slot_id = (run.workspace_anchor or {}).get("slot_id")
        slot = await session.get(WorkspaceSlot, slot_id) if slot_id else None
        if slot is None:
            return
        root = Path(slot.root_path).resolve()
        attempts = await session.scalars(select(ToolExecutionAttempt).where(
            ToolExecutionAttempt.run_id == run.run_id, ToolExecutionAttempt.status == "completed"))
        for attempt in attempts:
            call = calls.get(attempt.call_id)
            if call is None or call.get("name") not in {"read_file", "write_file"}:
                continue
            args = call.get("args") or {}
            if not isinstance(args.get("path"), str):
                continue
            execution_call = {**call, "args": {**args, "path": str(canonical_target(root, args["path"]))}}
            if attempt.call_hash != content_hash(execution_call):
                continue
            result = deserialize_history_message(attempt.result) if attempt.result else None
            if result is None or result.status != "success" or result.tool_call_id != attempt.call_id or result.name != call["name"]:
                continue
            observed = await asyncio.to_thread(self._observe, root, call, result.content)
            if observed is None:
                continue
            await DomainResultRepository().record(session, kind="artifact",
                source_id=canonical_hash([run.run_id, attempt.call_id, observed["path"]]),
                loop_id=run.loop_id, context_id=run.task_id, run_id=run.run_id,
                payload={**observed, "workspace_revision": run.workspace_result["revision"],
                    "execution_proof": {"run_id": run.run_id, "call_id": attempt.call_id,
                        "attempt_id": attempt.attempt_id, "call_hash": attempt.call_hash,
                        "tool_name": call["name"], "workspace": str(root), "bound": True}},
                audit={"tool_attempt_id": attempt.attempt_id, "final_checkpoint_id": run.final_checkpoint_id})

    @staticmethod
    def _observe(root, call, output):
        args = call.get("args") or {}
        if not isinstance(args.get("path"), str):
            return None
        path = canonical_target(root, args["path"])
        if not path.is_relative_to(root) or not path.is_file():
            return None
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        text = raw.decode("utf-8", errors="replace")
        expected = output if call["name"] == "read_file" else args.get("content")
        if not isinstance(expected, str):
            return None
        matches = text == expected if call["name"] == "read_file" else text.replace("\r\n", "\n") == expected.replace("\r\n", "\n")
        if not matches:
            return None
        return {"path": path.relative_to(root).as_posix(), "sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}
