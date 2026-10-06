"""本文件对外提供 PatrolReadView 与 PatrolWorkerReadRequest 的冻结认知读面和精确分页合同。

输入为完整冻结 Observation、Worker 请求身份/hash、UTF-8 游标和页容量；输出为来源目录、同政策等待准入读面或精确 JSON 原文页面。
具体工作流为将 Worker 正文转换成身份目录及摘要，复用 ClarificationFacts/Policy 投影当前合法等待身份，完整事实不修改；
分页核验冻结 hash 和游标边界，返回可连续重建正文的 next_cursor，准入读面不提交动作或替代 Kernel 复检。
历史来源明确标记其所属轮次，不证明当前准备完成；当前合法候选仍由完整内部输入进行派生验证。
示例：view = PatrolReadView.payload(observation)；page = PatrolReadView.page(worker, request)。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.clarification_admission import ClarificationAdmissionPolicy, ClarificationFacts
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash


class PatrolWorkerReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["worker"] = "worker"
    request_id: str = Field(min_length=1, max_length=120)
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    cursor: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=8192, ge=256, le=65536)


class PatrolReadView:
    @classmethod
    def payload(cls, observation, sources=None) -> dict:
        payload = EffectiveMissionProjector.observation_payload(observation)
        payload["clarification_admission"] = ClarificationAdmissionPolicy.read_view(ClarificationFacts.from_observation(observation))
        payload["worker_results"] = [cls._descriptor(item, observation.round_id) for item in observation.worker_results]
        payload["worker_source_catalog"] = [cls._descriptor(item, observation.round_id) for item in (sources if sources is not None else observation.worker_results)]
        return payload

    @classmethod
    def _descriptor(cls, worker: dict, round_id: str) -> dict:
        result = worker.get("result") or {}
        return {
            **{key: value for key, value in worker.items() if key != "result"},
            "result_hash": canonical_hash(result),
            "result_bytes": len(cls._encoded(result)),
            "current_round": worker.get("round_id") == round_id,
            "read": {"source": "worker", "request_id": worker["request_id"],
                     "result_hash": canonical_hash(result), "cursor": 0},
            "result_summary": {
                "fields": sorted(result),
                "work_spec_ids": [item.get("work_spec_id") or item.get("spec_id") for item in result.get("work_specs", ()) if isinstance(item, dict)],
                "check_results": result.get("checks", result.get("criteria", ())),
                "conclusion": result.get("conclusion"),
                "unresolved": result.get("unresolved", ()),
            },
        }

    @classmethod
    def page(cls, worker: dict, request: PatrolWorkerReadRequest) -> dict:
        result = worker.get("result") or {}
        if worker.get("request_id") != request.request_id or canonical_hash(result) != request.result_hash:
            raise ValueError("Patrol Worker 来源 identity/hash 不匹配")
        encoded = cls._encoded(result)
        if request.cursor > len(encoded):
            raise ValueError("Patrol Worker 游标超出冻结来源")
        try:
            encoded[:request.cursor].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Patrol Worker 游标不在 UTF-8 边界") from exc
        content = encoded[request.cursor:request.cursor + request.max_bytes].decode("utf-8", errors="ignore")
        end = request.cursor + len(content.encode("utf-8"))
        return {"source": "worker", "request_id": request.request_id,
                "round_id": worker.get("round_id"), "result_hash": request.result_hash,
                "cursor": request.cursor, "next_cursor": end if end < len(encoded) else None,
                "total_bytes": len(encoded), "content": content}

    @staticmethod
    def _encoded(value) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
