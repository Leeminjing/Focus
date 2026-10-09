"""本文件对外提供 initialize_task_state 与 publish_initial_portfolio 的共同初始化端口。

输入为调用方事务、Loop、用户 Mission/既有 Progress、Lane 和已发布 Context Revision；输出为初始 Mission/P0 或首代 Portfolio。
具体工作流为复用 Mission/Progress 仓库登记初始任务状态，再冻结精确来源、登记 unchanged candidate、切换 Program/Loop 指针并发布真实 Lineage，
不创建 Run 或模型产物。示例：await publish_initial_portfolio(session, loop, lane, revision)。
"""

from datetime import UTC, datetime
import uuid
from focus.history import content_hash
from backend.app.desktop.context_curation.models import (
    CurationProgram,
    PortfolioRevision,
    PortfolioLaneCandidate,
)
from backend.app.desktop.agent_loop.lineage_events import ContextLineageEventRecorder


async def publish_initial_portfolio(session, loop, lane, revision, *, additional=()):
    pairs = ((lane, revision), *additional)
    frontier = [item.ref.model_dump(mode="json") for _, item in pairs]
    digest = content_hash(frontier)
    portfolio = PortfolioRevision(
        portfolio_revision_id=uuid.uuid4().hex,
        program_id=loop.program_id,
        generation=1,
        source_frontier=frontier,
        frontier_hash=digest,
        base_program_revision=1,
        control_revisions={
            "loop_revision": loop.revision,
            "grant_revision": loop.authority_revision,
            "workspace_revision": "1",
        },
        target_lanes=[{"lane_id": item.lane_id, "action": "keep"} for item, _ in pairs],
        status="published",
        completed_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
    )
    session.add(portfolio)
    await session.flush()
    for item, source in pairs:
        session.add(
            PortfolioLaneCandidate(
                candidate_id=uuid.uuid4().hex,
                portfolio_revision_id=portfolio.portfolio_revision_id,
                lane_id=item.lane_id,
                action="keep",
                target_context_id=source.ref.context_id,
                base_publisher_epoch=item.publisher_epoch,
                base_context_revision_id=source.ref.revision_id,
                candidate_context_revision_id=source.ref.revision_id,
                purpose=item.purpose,
                source_allocation=frontier,
                source_frontier_hash=digest,
                semantic_fingerprint=source.content_hash,
                status="unchanged",
            )
        )
        item.current_source_frontier_hash = digest
        item.current_semantic_fingerprint = source.content_hash
    program = await session.get(CurationProgram, loop.program_id)
    program.current_portfolio_revision_id = portfolio.portfolio_revision_id
    loop.current_portfolio_revision_id = portfolio.portfolio_revision_id
    await session.flush()
    await ContextLineageEventRecorder().record_loop_members(session, loop_id=loop.loop_id)
    return portfolio


async def initialize_task_state(
    session,
    loop,
    contract,
    *,
    legacy_goal_revision_id=None,
    document=None,
    input_sources=None,
    source_keys=(),
):
    from backend.app.desktop.agent_loop.mission_service import MissionRevisionService
    from backend.app.desktop.agent_loop.task_progress.repository import TaskProgressRepository
    from backend.app.desktop.agent_loop.task_progress.consolidation import initial_progress

    await MissionRevisionService().record(
        session,
        loop_id=loop.loop_id,
        revision=loop.goal_revision,
        contract=contract,
        authored_by="user",
        legacy_goal_revision_id=legacy_goal_revision_id,
        input_sources=input_sources,
    )
    return await TaskProgressRepository().initialize(
        session,
        loop.loop_id,
        document or initial_progress(contract.model_dump(mode="json"), loop.goal_revision),
        source_keys=source_keys,
    )
