"""本文件对外提供 RunOutcomeRecorder 与 workspace_results 的领域结果适配。

输入为终态 Run、其指令之后的已确认输出和已提交 Workspace 结果；输出为持久领域结果身份。
具体工作流为排除继承消息，提取测试结论与 Agent 最终自述，再单独保存 Workspace 变化；ArtifactObservationRecorder 从真实文件调用审计和当前文件记录产物，既有显式产物列表仍独立保留；
原始消息与 checkpoint identity 只保存在 audit。可信报告的命名结果使用注入的实际秘密值脱敏，Run success 不被翻译为任务完成。
示例：await RunOutcomeRecorder().record(session, run, checkpoint_messages)。
Workspace 适配只提取状态、版本、文件与指纹变更，路径身份哈希保证字段长度稳定。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.domain_evidence.tests import TestResultParser, test_identity
from backend.app.desktop.domain_evidence.artifacts import ArtifactObservationRecorder
from backend.app.desktop.secret_redaction import configured_secret_values


def workspace_results(result: dict) -> tuple[dict, ...]:
    output = []
    if result:
        payload = {
            key: result[key]
            for key in ("workspace_status", "revision", "adoption_state")
            if key in result
        }
        payload["effects"] = [
            {
                key: effect[key]
                for key in (
                    "kind",
                    "changed",
                    "changed_files",
                    "base_revision",
                    "target_revision",
                )
                if key in effect
            }
            for effect in result.get("effect_evidence") or ()
            if isinstance(effect, dict)
            and effect.get("kind")
            in {"workspace_fingerprint_transition", "restart_workspace_reconciliation"}
        ]
        output.append({"kind": "workspace", "payload": payload})
    for key in ("artifacts", "artifact_paths", "changed_files", "output_files"):
        for item in result.get(key) or ():
            path = (
                item
                if isinstance(item, str)
                else str(item.get("path") or item.get("name") or "")
                if isinstance(item, dict)
                else ""
            )
            if path:
                output.append({"kind": "artifact", "payload": {"path": path}})
    return tuple(output)


class RunOutcomeRecorder:
    def __init__(self, *, secrets=None) -> None:
        self._results = DomainResultRepository()
        self._secrets = configured_secret_values() if secrets is None else secrets

    async def record(
        self, session: AsyncSession, run: Any, messages: tuple[dict, ...]
    ) -> None:
        if run.kind != "main":
            return
        anchor = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("id") == run.origin_message_id
            ),
            None,
        )
        additions = messages[anchor + 1 :] if anchor is not None else ()
        outputs = [
            item
            for item in additions
            if str(item.get("role") or item.get("type")) in {"ai", "assistant"}
            and not item.get("tool_calls")
        ]
        statement = (
            outputs[-1].get("content")
            if outputs and isinstance(outputs[-1].get("content"), str)
            else None
        )
        await self._results.record(
            session,
            kind="run_outcome",
            source_id=run.run_id,
            loop_id=run.loop_id,
            context_id=run.task_id,
            run_id=run.run_id,
            payload={
                "status": run.status,
                "statement": statement,
                "support": "asserted" if statement else "unknown",
            },
            audit={
                "final_checkpoint_id": run.final_checkpoint_id,
                "message_id": outputs[-1].get("id") if outputs else None,
                "anchor_found": anchor is not None,
            },
        )
        calls = {
            str(call.get("id")): call
            for item in additions
            for call in item.get("tool_calls") or ()
            if isinstance(call, dict)
        }
        for item in additions:
            if str(item.get("role") or item.get("type")) != "tool" or not isinstance(
                item.get("content"), str
            ):
                continue
            call_id = str(item.get("tool_call_id") or item.get("id") or "")
            call = calls.get(call_id) or {}
            args = call.get("args") or {}
            parsed = TestResultParser.parse(
                item.get("name") or call.get("name"),
                item["content"],
                command=str(args.get("command") or args.get("cmd") or "")
                if isinstance(args, dict)
                else None,
                execution_status=item.get("status"),
                run_id=run.run_id,
                call_id=call_id,
                secrets=self._secrets,
            )
            if parsed is not None and call_id:
                await self._results.record(
                    session,
                    kind="test",
                    source_id=test_identity(run.run_id, call_id),
                    payload={**parsed, "workspace_revision": (run.workspace_result or {}).get("revision")},
                    loop_id=run.loop_id,
                    context_id=run.task_id,
                    run_id=run.run_id,
                    audit={
                        "tool_call_id": call_id,
                        "message_id": item.get("id"),
                        "final_checkpoint_id": run.final_checkpoint_id,
                    },
                )
        for result in workspace_results(dict(run.workspace_result or {})):
            source_id = canonical_hash(
                [
                    run.run_id,
                    result["kind"],
                    result["payload"].get("path") or "workspace",
                ]
            )
            await self._results.record(
                session,
                kind=result["kind"],
                source_id=source_id,
                payload=result["payload"],
                loop_id=run.loop_id,
                context_id=run.task_id,
                run_id=run.run_id,
            )
        await ArtifactObservationRecorder().record(session, run, calls)
