r"""本文件对外提供 LoopFactBuilder 与 TestResultParser 的确定性事实构造能力。

输入为持久化 Run、workspace execution anchor、结构化 workspace result 和 Run 新增 ToolMessage；输出为
run、workspace、artifact 与 test 领域事实；工具轨迹不成为 LoopFact。具体工作流为只接受权威字段和明确测试框架证据，保留
Run/revision/message 锚点并拒绝从普通错误文本猜测测试。示例：`LoopFactBuilder.run_fact(run)`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor

_ARTIFACT_KEYS = frozenset(
    {"artifacts", "artifact_paths", "changed_files", "files", "outputs", "output_files"}
)


class LoopFactBuilder:
    @classmethod
    def run_fact(cls, run: DesktopRun) -> dict:
        return {
            "fact_id": cls.fact_id("run", run.run_id),
            "context_id": run.task_id,
            "kind": "run",
            "status": run.status,
            "title": f"Agent Run {run.status}",
            "summary": cls._run_summary(run),
            "metrics": {
                "model_calls": run.model_call_count,
                "input_tokens": run.prompt_input_tokens,
                "output_tokens": run.prompt_output_tokens,
            },
            "evidence": cls._run_evidence(run),
            "occurred_at": cls._occurred_at(run),
        }

    @classmethod
    def workspace_facts(
        cls,
        run: DesktopRun,
        anchor: RunExecutionAnchor | None,
    ) -> list[dict]:
        result = dict(run.workspace_result or {})
        effects = list(anchor.effect_evidence or []) if anchor is not None else list(
            result.get("effect_evidence") or []
        )
        facts: list[dict] = []
        if anchor is not None or effects or result.get("workspace_status"):
            changed = cls._workspace_changed(anchor, effects)
            facts.append(
                {
                    "fact_id": cls.fact_id("workspace", run.run_id),
                    "context_id": run.task_id,
                    "kind": "workspace",
                    "status": "changed" if changed else str(result.get("workspace_status") or "verified"),
                    "title": "Workspace 变化" if changed else "Workspace 核验",
                    "summary": cls._workspace_summary(anchor, result, changed),
                    "metrics": {"changed": changed},
                    "evidence": {
                        **cls._run_evidence(run),
                        "slot_id": anchor.slot_id if anchor is not None else None,
                        "observed_revision": anchor.observed_workspace_revision if anchor is not None else None,
                        "resulting_revision": anchor.resulting_workspace_revision if anchor is not None else result.get("revision"),
                        "effects": effects,
                    },
                    "occurred_at": cls._occurred_at(run),
                }
            )
        for position, artifact in enumerate(cls.artifacts(result, effects)):
            facts.append(
                {
                    "fact_id": cls.fact_id("artifact", run.run_id, artifact),
                    "context_id": run.task_id,
                    "kind": "artifact",
                    "status": "verified",
                    "title": "执行产物",
                    "summary": artifact,
                    "metrics": {},
                    "evidence": {
                        **cls._run_evidence(run),
                        "artifact": artifact,
                        "position": position,
                    },
                    "occurred_at": cls._occurred_at(run),
                }
            )
        return facts

    @classmethod
    def test_facts(
        cls,
        run: DesktopRun,
        revision_id: str,
        messages: Iterable[dict[str, Any]],
    ) -> list[dict]:
        facts: list[dict] = []
        for index, message in enumerate(messages):
            if cls.message_role(message) != "tool":
                continue
            content = cls.message_content(message.get("content"))
            message_id = str(message.get("id") or f"index-{index}")
            tool_name = str(message.get("name") or "") or None
            base = {
                "context_id": run.task_id,
                "status": cls.tool_status(message, content),
                "evidence": {
                    **cls._run_evidence(run),
                    "context_revision_id": revision_id,
                    "message_id": message.get("id"),
                    "message_index": index,
                    "tool_call_id": message.get("tool_call_id"),
                    "tool_name": tool_name,
                },
                "occurred_at": cls._occurred_at(run),
            }
            test = TestResultParser.parse(tool_name, content)
            if test is not None:
                facts.append(
                    {
                        **base,
                        "status": test["status"],
                        "fact_id": cls.fact_id("test", run.run_id, revision_id, message_id),
                        "kind": "test",
                        "title": "测试结果",
                        "summary": test["summary"],
                        "metrics": test["metrics"],
                    }
                )
        return facts

    @classmethod
    def artifacts(cls, result: dict, effects: Iterable[dict]) -> list[str]:
        values: list[str] = []
        cls._collect_artifacts(result, values)
        for effect in effects:
            if isinstance(effect, dict):
                cls._collect_artifacts(effect, values)
        return list(dict.fromkeys(item for item in values if item))

    @staticmethod
    def message_role(message: dict[str, Any]) -> str:
        return str(message.get("role") or message.get("type") or "").lower()

    @staticmethod
    def message_content(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in value
            ).strip()
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def tool_status(message: dict[str, Any], content: str) -> str:
        declared = str(message.get("status") or "").lower()
        if declared in {"error", "failed", "failure"}:
            return "failed"
        return "verified"

    @staticmethod
    def fact_id(*parts: str) -> str:
        return hashlib.sha256(":".join(parts).encode()).hexdigest()[:24]

    @classmethod
    def _collect_artifacts(cls, payload: dict[str, Any], output: list[str]) -> None:
        for key, value in payload.items():
            if key not in _ARTIFACT_KEYS:
                continue
            cls._append_artifact_value(value, output)

    @classmethod
    def _append_artifact_value(cls, value: Any, output: list[str]) -> None:
        if isinstance(value, str):
            output.append(value)
            return
        if isinstance(value, list):
            for item in value:
                cls._append_artifact_value(item, output)
            return
        if isinstance(value, dict):
            identity = value.get("path") or value.get("name") or value.get("uri")
            if identity:
                output.append(str(identity))

    @staticmethod
    def _workspace_changed(
        anchor: RunExecutionAnchor | None,
        effects: Iterable[dict],
    ) -> bool:
        if anchor is not None and anchor.resulting_fingerprint and anchor.resulting_fingerprint != anchor.observed_fingerprint:
            return True
        return any(isinstance(effect, dict) and effect.get("changed") is True for effect in effects)

    @staticmethod
    def _workspace_summary(
        anchor: RunExecutionAnchor | None,
        result: dict,
        changed: bool,
    ) -> str:
        revision = (
            anchor.resulting_workspace_revision if anchor is not None else result.get("revision")
        )
        state = "发生变化" if changed else "未检测到变化"
        return f"Workspace {state}" + (f" · revision {revision}" if revision is not None else "")

    @staticmethod
    def _run_summary(run: DesktopRun) -> str:
        if run.error:
            return str(run.error)[:800]
        result = run.workspace_result or {}
        if result:
            return json.dumps(result, ensure_ascii=False, sort_keys=True)[:800]
        return f"Run 使用 {run.model_call_count} 次模型调用后进入 {run.status}"

    @staticmethod
    def _run_evidence(run: DesktopRun) -> dict:
        return {
            "run_id": run.run_id,
            "round_id": run.round_id,
            "context_revision_id": run.context_revision_id,
            "final_checkpoint_id": run.final_checkpoint_id,
            "workspace_anchor": run.workspace_anchor,
            "workspace_result": run.workspace_result,
            "error": run.error,
        }

    @staticmethod
    def _occurred_at(run: DesktopRun) -> str:
        return (run.settled_at or run.created_at).isoformat()
