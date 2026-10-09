"""本文件对外提供 TaskProgressRuntime 的独立持久后台沉淀和恢复入口。

输入为 sessions、结构化模型配置及 frozen work；输出为每份 Observation 唯一的不可变后继，或可诊断 blocked。
具体工作流为按可注入有限政策在短事务领取和续租 fence/lease，事务外有界模型解释冻结来源，失去租约立即取消请求并等待清理，验证 patch 后原子发布；
每次真实调用独立记账；候选合同绑定冻结来源，按尝试保存安全校验诊断并在下次调用反馈修正；显式重试可绑定新预算授权版本，不改变三项冻结输入。
终态 Loop 的冻结工作仍可收口但不会启动执行。
示例：await runtime.drain()；retry(observation_id) 显式恢复预算/证据 blocker。
预算在调用前核对共享未结算预留并预留输入/输出与调用量；空上限表示不限总量，报告真实用量后替换估计，未报告与 crash 保留保守预留；预留和回填在同事务发布共享 accounting 读模型。
生产模型复用 StructuredWorkerModel 的实际请求估算；注入模型若无估算端口，按完整消息与额外 schema 做通用估算，不拥有 Provider 序列化规则。
已受理 workspace 问题/设想不自动成为确认目标；缺省 Mission 不生成待办或验收通过。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from focus.config.app_config import AppConfig
from focus.messages.request_budget import estimate_request_budget
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopDelegationGrant,
    LoopObservation,
)
from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from backend.app.desktop.agent_loop.task_progress.consolidation import (
    TaskProgressConsolidator,
)
from backend.app.desktop.agent_loop.task_progress.contracts import CandidateIssue, ProgressCandidate
from backend.app.desktop.agent_loop.task_progress.candidate_contract import CandidateValidationError, candidate_schema, structural_error
from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload, validation_diagnostic
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork
from backend.app.desktop.agent_loop.task_progress.execution_policy import ProgressExecutionPolicy
from backend.app.desktop.agent_loop.task_progress.work_lease import ProgressWorkLease
from backend.app.desktop.agent_loop.task_progress.repository import (
    TaskProgressRepository,
)
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.resource_limits import exceeds_limit

_SYSTEM = """你负责一次 Round 边界的任务记忆沉淀，没有执行或拓扑提交权。
previous_progress 是已有完整任务记忆；task_delta 是本次冻结且尚未吸收的任务领域增量。
只输出新增或有证据支持的修改，保留未出现的旧成果、义务和 blocker。修改旧 item 必须保持 item_id 并以 corrects 引用它；真正替代其他事项用 supersedes 引用前序 identity，旧项保留为不再适用。
evidence_keys 只能引用本次冻结 source_key。来源冲突标记 conflicted/unknown，不根据最新文本简单覆盖。
Run success 仅表示执行结束；run_outcome.statement 仅是 asserted。任务 completed 必须有相关独立领域证据；失败或未知测试不支持 completed。
进度语义只含任务成果、状态、blocker 和未决项，不复制 Tool 调用、原始消息、checkpoint、代码正文或控制预算。
用户目标变化属于义务修订，不是成果。每条 source 必须被 changes 引用，或在 source_assessments 明确解释。
workspace_input 是收到的用户信息；问题、设想和探索不自动形成确认要求。Mission revision 是已提交的有效分区，缺省 outcome/check 不制造目标、待办或验收通过。
workspace_input 无 decision disposition 时只能保留未决信息，不改写原有效事项，不标记已开展或已完成；Kernel 的同源后继版本记录实际处置。
无法判断的来源以 unknown 保留未决事项；already_known/not_task_progress 必须说明理由，不允许遗漏后直接吸收。
validation_feedback 是服务器对上一候选的校验诊断，不是新的任务来源或授权。按 code/path 修正错误，不沿用越界引用，不更改 task_delta。
输出符合给定 schema；不要输出私有思维链。"""
_logger = logging.getLogger(__name__)


class TaskProgressRuntime:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        app_config: AppConfig,
        *,
        model_factory=None,
        policy: ProgressExecutionPolicy | None = None,
    ) -> None:
        self._sessions = sessions
        self._config = app_config
        self._policy = policy or ProgressExecutionPolicy()
        self._lease = ProgressWorkLease(sessions, self._policy)
        self._models = model_factory or (
            lambda name: StructuredWorkerModel(app_config, name)
        )
        self._repository = TaskProgressRepository()
        self._consolidator = TaskProgressConsolidator()

    async def drain(self, *, loop_id: str | None = None) -> int:
        claim = await self._claim(loop_id=loop_id)
        if claim is None:
            return 0
        observation_id, fence = claim
        try:
            await self._lease.run(observation_id, fence, lambda: self._consolidate(observation_id, fence))
        except asyncio.CancelledError:
            raise
        except CandidateValidationError as exc:
            _logger.warning("任务记忆候选校验失败 observation=%s code=%s", observation_id, exc.issues[0].code)
            await self._fail(observation_id, fence, exc)
        except Exception as exc:
            _logger.exception("任务记忆沉淀失败 observation=%s", observation_id)
            await self._fail(observation_id, fence, exc)
        return 1

    async def _consolidate(self, observation_id: str, fence: int) -> None:
        async with self._sessions() as session:
            inputs = await self._repository.inputs(session, observation_id)
            observation = await session.get(LoopObservation, observation_id)
            envelope = dict(observation.envelope)
            model_name = (envelope.get("budget") or {}).get("progress_model_name")
            work = await session.get(LoopProgressWork, observation_id)
            payload = interpretation_payload(inputs, envelope.get("mission") or envelope.get("goal"), work.attempt_events, work.error)
        candidate = ProgressCandidate()
        try:
            if inputs.task_delta.sources:
                candidate = await self._interpret(inputs, fence, envelope, model_name, payload)
            result, contribution = self._consolidator.apply(inputs, candidate, envelope.get("mission") or {})
        except CandidateValidationError as error:
            await self._record_validation(inputs, fence, error.candidate, error.issues)
            raise
        await self._record_validation(inputs, fence, candidate, ())
        async with self._sessions.begin() as session:
            await self._repository.publish(session, observation_id, fence, result, contribution)

    async def _record_validation(self, inputs, fence, candidate, issues):
        diagnostic = validation_diagnostic(inputs, fence, candidate, issues)
        async with self._sessions.begin() as session:
            await self._repository.record_validation(session, inputs, fence, diagnostic)

    async def _interpret(self, inputs, fence, envelope, model_name, payload):
        model = self._models(model_name)
        schema = candidate_schema(inputs)
        estimator = getattr(model, "estimate_input_tokens", None)
        if estimator is not None:
            estimated_input = estimator(schema, _SYSTEM, payload)
        else:
            messages = StructuredWorkerModel.request_messages(schema, _SYSTEM, payload)
            estimated_input = estimate_request_budget(messages, format_spec=schema.model_json_schema())
        window = model.context_window_tokens
        if (
            window is not None
            and estimated_input + model.max_output_tokens > window
        ):
            raise ValueError(
                "progress_memory_context_budget: 完整输入超出模型窗口"
            )
        await self._reserve(
            inputs.observation_id,
            fence,
            envelope,
            estimated_input,
            model.max_output_tokens,
        )
        try:
            return await asyncio.wait_for(
                model.invoke(schema, _SYSTEM, payload), timeout=self._policy.request_seconds
            )
        except ValidationError as error:
            raise structural_error(error) from None
        except json.JSONDecodeError:
            raise CandidateValidationError((CandidateIssue(code="schema_invalid_json", path=("candidate",)),)) from None
        finally:
            await asyncio.shield(
                self._record_usage(inputs.observation_id, fence, model)
            )

    async def _claim(self, *, loop_id: str | None = None) -> tuple[str, int] | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            query = select(LoopProgressWork).where(
                or_(
                    LoopProgressWork.state == "pending",
                    (LoopProgressWork.state == "claimed")
                    & (LoopProgressWork.lease_expires_at < now),
                )
            )
            if loop_id is not None:
                query = query.where(LoopProgressWork.loop_id == loop_id)
            work = await session.scalar(
                query.order_by(
                    LoopProgressWork.created_at, LoopProgressWork.observation_id
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if work is None:
                return None
            if work.attempts >= self._policy.max_attempts:
                work.state = "blocked"
                work.error = work.error or "progress_memory_attempts_exhausted"
                work.lease_expires_at = None
                return None
            work.state = "claimed"
            work.fence += 1
            work.attempts += 1
            work.lease_expires_at = now + timedelta(seconds=self._policy.lease_seconds)
            work.attempt_events = [
                *work.attempt_events,
                {
                    "event": "claimed",
                    "fence": work.fence,
                    "attempt": work.attempts,
                    "at": now.isoformat(),
                },
            ]
            return work.observation_id, work.fence

    async def _reserve(
        self,
        observation_id: str,
        fence: int,
        envelope: dict,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        limits = (envelope.get("budget") or {}).get("limits") or {}
        async with self._sessions.begin() as session:
            await session.get(AgentLoop, envelope["loop_id"], with_for_update=True)
            usage = await session.get(
                LoopBudgetUsage, envelope["loop_id"], with_for_update=True
            )
            work = await session.get(
                LoopProgressWork, observation_id, with_for_update=True
            )
            if work.state != "claimed" or work.fence != fence or work.lease_expires_at is None or work.lease_expires_at <= datetime.now(UTC):
                raise ValueError("progress_memory_worker_superseded")
            authorization = work.retry_budget_authorization
            if authorization is not None:
                grant = await session.get(
                    LoopDelegationGrant, authorization["grant_id"]
                )
                loop = await session.get(AgentLoop, envelope["loop_id"])
                if (
                    grant is None
                    or grant.loop_id != loop.loop_id
                    or grant.revision != authorization["grant_revision"]
                    or grant.revision != loop.authority_revision
                    or grant.status != "active"
                    or (
                        grant.expires_at is not None
                        and grant.expires_at <= datetime.now(UTC)
                    )
                ):
                    raise ValueError(
                        "progress_memory_retry_budget_authorization_changed"
                    )
                limits = authorization["limits"]
            if usage is not None:
                pending = await LoopUsageLedger.pending_reservations(session, envelope["loop_id"])
                for field, projected in (
                    ("model_calls", 1),
                    ("input_tokens", input_tokens),
                    ("output_tokens", output_tokens),
                ):
                    if exceeds_limit(int(getattr(usage, field)) + pending[field] + projected,
                                     limits.get(f"max_{field}")):
                        raise ValueError(f"progress_memory_{field}_budget")
                LoopUsageLedger.apply(
                    usage,
                    LoopUsageDelta(
                        model_calls=1,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        retries=int(work.attempts > 1),
                    ),
                )
            work.usage = [
                *work.usage,
                {
                    "fence": fence,
                    "reserved_calls": 1,
                    "estimated_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "usage_reported": False,
                    "budget_authorization": authorization,
                },
            ]
            from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

            await LoopAccountingEventRecorder().record(session, work.loop_id,
                source_kind="progress_memory", source_id=f"{observation_id}:{fence}", transition="reserved")

    async def _record_usage(self, observation_id: str, fence: int, model) -> None:
        async with self._sessions.begin() as session:
            identity = await session.get(LoopProgressWork, observation_id)
            await session.get(AgentLoop, identity.loop_id, with_for_update=True)
            usage = await session.get(
                LoopBudgetUsage, identity.loop_id, with_for_update=True
            )
            work = await session.get(
                LoopProgressWork,
                observation_id,
                with_for_update=True,
                populate_existing=True,
            )
            entries = [dict(item) for item in work.usage]
            entry = next((item for item in entries if item["fence"] == fence), None)
            if entry is None or entry.get("accounted"):
                return
            measured = model.last_usage
            if usage is not None:
                LoopUsageLedger.apply(
                    usage, LoopUsageDelta(model_calls=max(0, measured.model_calls - 1))
                )
                if model.last_usage_reported:
                    usage.input_tokens = max(
                        0,
                        usage.input_tokens
                        - entry["estimated_input_tokens"]
                        + measured.input_tokens,
                    )
                    usage.output_tokens = max(
                        0,
                        usage.output_tokens
                        - entry["reserved_output_tokens"]
                        + measured.output_tokens,
                    )
            entry.update(
                {
                    "accounted": True,
                    "model_calls": max(1, measured.model_calls),
                    "input_tokens": measured.input_tokens,
                    "output_tokens": measured.output_tokens,
                    "usage_reported": model.last_usage_reported,
                }
            )
            work.usage = entries
            from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

            await LoopAccountingEventRecorder().record(session, work.loop_id,
                source_kind="progress_memory", source_id=f"{observation_id}:{fence}",
                transition="actual" if model.last_usage_reported else "unknown")

    async def _fail(self, observation_id: str, fence: int, error: Exception) -> None:
        async with self._sessions.begin() as session:
            work = await session.get(
                LoopProgressWork, observation_id, with_for_update=True
            )
            if (work is not None and work.fence == fence and work.state == "claimed"
                    and work.lease_expires_at is not None and work.lease_expires_at > datetime.now(UTC)):
                work.error = f"{type(error).__name__}: {error}"[:2000]
                work.lease_expires_at = None
                work.state = (
                    "blocked"
                    if work.attempts >= self._policy.max_attempts or "budget" in str(error)
                    else "pending"
                )
                work.attempt_events = [
                    *work.attempt_events,
                    {
                        "event": work.state,
                        "fence": fence,
                        "failure_kind": type(error).__name__,
                        "at": datetime.now(UTC).isoformat(),
                    },
                ]

    async def retry(self, observation_id: str, *, loop_id: str | None = None) -> None:
        async with self._sessions.begin() as session:
            await self._repository.retry(session, observation_id, loop_id=loop_id)
