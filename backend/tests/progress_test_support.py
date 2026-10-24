"""本文件提供 register_progress，准备独立任务记忆测试所需的成功授权边界。

输入为测试 session 工厂与真实冻结 Observation；输出为独立 work identity。
工作流在短事务写入成功决策及 Round 指针后调用生产队列，不执行模型或业务动作。
示例：work_id = await register_progress(sessions, observation)；Kernel 原子性由专门集成测试验证。
"""

from datetime import UTC, datetime
import uuid

from backend.app.desktop.patrol.agent_loop.models import LoopDecision, LoopRound
from backend.app.desktop.patrol.agent_loop.task_progress.work_queue import TaskProgressQueue


async def register_progress(sessions, observation):
    async with sessions.begin() as session:
        round_row = await session.get(LoopRound, observation.round_id)
        if round_row.decision_id is None:
            identity = uuid.uuid4().hex
            session.add(LoopDecision(decision_id=identity, loop_id=observation.loop_id,
                round_id=observation.round_id, observation_id=observation.decision_inputs_ref,
                observation_generation=observation.generation, authority_committed_at=datetime.now(UTC),
                holder_id="progress-test", intent={}, rationale="记忆边界测试", evidence=[],
                status="committed", idempotency_key=identity))
            await session.flush()
            round_row.decision_id = identity
            await session.flush()
        work = await TaskProgressQueue().enqueue_observation(session, observation.loop_id, observation.decision_inputs_ref)
        return work.work_id
