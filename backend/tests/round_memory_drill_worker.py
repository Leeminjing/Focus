"""本文件提供隔离演练子进程的记忆、展示、旧版本读取和 Portfolio 发布入口。

输入为命令行模式、Loop identity、信号目录与独立测试库环境变量；输出为原子 JSON 阶段信号。
工作流为每个进程独立建立 engine/session，调用生产服务，仅在模型端口或提交边界注入故障。
冻结认知模式在 Curator 补充持久化后等待，重启模式重新装配 Patrol 输入并返回原身份、哈希与补充。
旧版本模式只读取暂停/停止的 Loop，不启动认知轮次。示例：python round_memory_drill_worker.py publish LOOP DIR。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _Signals:
    def __init__(self, directory: Path):
        self._directory = directory

    def emit(self, name, **payload):
        target = self._directory / f"{name}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"pid": os.getpid(), **payload}), encoding="utf-8"
        )
        temporary.replace(target)

    async def hold(self):
        async with asyncio.timeout(45):
            while not (self._directory / "release").exists():
                await asyncio.sleep(0.03)


class _Checkpointer:
    async def aget_tuple(self, config):
        return SimpleNamespace(
            config=config, checkpoint={"channel_values": {"messages": []}}, metadata={}
        )


async def _memory(sessions, args, signals):
    from focus.runtime.runs.usage import ModelUsage

    from backend.app.desktop.agent_loop.task_progress.contracts import (
        ProgressCandidate,
        SourceAssessment,
        canonical_hash,
    )
    from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime

    class Model:
        context_window_tokens = 100000
        max_output_tokens = 1000
        last_usage = ModelUsage(model_calls=1, input_tokens=11, output_tokens=7)
        last_usage_reported = False

        async def invoke(self, contract, system, payload):
            signals.emit("model_started", input_hash=canonical_hash(payload))
            if args.mode == "hold-model":
                await signals.hold()
            self.last_usage_reported = True
            if args.mode == "fail-model":
                raise ValueError("drill_memory_model_failure")
            return ProgressCandidate(
                source_assessments=tuple(
                    SourceAssessment(
                        source_key=source["source_key"],
                        disposition="unknown",
                        explanation="演练保留未决领域来源",
                    )
                    for source in payload["task_delta"]["sources"]
                )
            )

    runtime = TaskProgressRuntime(
        sessions, SimpleNamespace(), model_factory=lambda _: Model()
    )
    if args.mode == "before-commit":
        publish = runtime._repository.publish

        async def held_publish(session, *values):
            result = await publish(session, *values)
            await session.flush()
            signals.emit("before_commit")
            await signals.hold()
            return result

        runtime._repository.publish = held_publish
    count = await runtime.drain(loop_id=args.loop_id)
    signals.emit("drained", count=count)
    if args.mode == "after-commit":
        signals.emit("after_commit")
        await signals.hold()


async def _facts(sessions, args, signals):
    from backend.app.desktop.agent_loop.fact_projector import FactProjector

    projector = FactProjector(sessions, _Checkpointer())
    materialize = projector._event_materializer.materialize

    async def injected(session, event):
        if event.event_id == args.entity_id:
            if args.mode == "hold-fact":
                signals.emit("fact_blocked", sequence=event.sequence)
                await signals.hold()
            if args.mode == "fail-fact":
                raise TimeoutError("drill_fact_timeout")
        return await materialize(session, event)

    projector._event_materializer.materialize = injected
    count = await (
        projector.repair_failure(args.entity_id)
        if args.mode == "repair-fact"
        else projector.project_loop(args.loop_id)
    )
    signals.emit("drained", count=count)


async def _legacy(sessions, args, signals):
    import backend.app.desktop.agent_loop.service as service_module
    from backend.app.desktop.agent_loop.models import AgentLoop

    async with sessions() as session:
        loop = await session.get(AgentLoop, args.loop_id)
        if loop.status not in {"paused", "stopped"}:
            raise ValueError("legacy drill requires a paused or stopped Loop")
    service = service_module.AgentLoopService(sessions)
    snapshot = await service.get(args.loop_id)
    events = await service.events(args.loop_id)
    signals.emit(
        "legacy_read",
        origin=str(Path(service_module.__file__).resolve()),
        status=snapshot["status"],
        event_count=len(events),
    )


async def _portfolio(sessions, args, signals):
    from backend.app.desktop.agent_loop.models import AgentLoop
    from backend.app.desktop.context_curation import (
        AtomicPortfolioPublisher,
        PortfolioControlRevisions,
        PortfolioRevision,
        PortfolioSuperseded,
    )
    from backend.app.desktop.context_evolution import ContextRevisionRepository

    async with sessions() as session:
        portfolio = await session.get(PortfolioRevision, args.entity_id)
        loop = await session.get(AgentLoop, args.loop_id)
        controls = PortfolioControlRevisions.model_validate(
            portfolio.control_revisions
        ).model_copy(
            update={
                "loop_revision": loop.revision,
                "grant_revision": loop.authority_revision,
            }
        )
    publisher = AtomicPortfolioPublisher(sessions, ContextRevisionRepository())
    try:
        await publisher.publish(args.entity_id, controls)
        outcome = "published"
    except PortfolioSuperseded:
        outcome = "superseded"
    signals.emit(
        "portfolio_result",
        outcome=outcome,
        recovery=await publisher.recover(args.entity_id),
    )


async def _frozen_cognition(sessions, args, signals):
    import uuid

    from backend.app.desktop.agent_loop.decision_context import (
        DecisionSupplementRepository,
        PatrolDecisionContext,
    )
    from backend.app.desktop.agent_loop.models import LoopObservation, LoopWorkerRequest
    from backend.app.desktop.agent_loop.observation import observation_hash
    from backend.app.desktop.agent_loop.observation_capture import (
        LoopObservationService,
    )
    from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime

    async with sessions() as session:
        stored = await session.get(LoopObservation, args.entity_id)
        round_id = stored.round_id
    observation = await LoopObservationService(sessions, _Checkpointer()).capture(
        args.loop_id, round_id
    )
    supplements = DecisionSupplementRepository()
    if args.mode == "hold-cognition":
        request = LoopWorkerRequest(
            worker_request_id=uuid.uuid4().hex,
            loop_id=args.loop_id,
            round_id=round_id,
            kind="lane_curator",
            scope={},
        )
        cognition, _, _ = await LoopWorkerRuntime(sessions, None)._evidence(request)
        async with sessions.begin() as session:
            await supplements.put(
                session,
                args.entity_id,
                "curator_results",
                {
                    "results": [
                        {
                            "kind": "proposal",
                            "frozen_ref": cognition["decision_inputs_ref"],
                        }
                    ]
                },
            )
    async with sessions() as session:
        supplement = await supplements.get(session, args.entity_id, "curator_results")
    combined = PatrolDecisionContext(
        observation, curator_results=tuple(supplement["results"])
    ).model_observation()
    signals.emit(
        "cognition_ready",
        base=observation.model_dump(mode="json"),
        base_hash=observation_hash(observation),
        combined=combined.model_dump(mode="json"),
        supplement=supplement,
    )
    if args.mode == "hold-cognition":
        await signals.hold()


async def _main(args):
    signals = _Signals(Path(args.directory))
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.mode in {"hold-cognition", "read-cognition"}:
            await _frozen_cognition(sessions, args, signals)
        elif args.mode == "legacy-probe":
            await _legacy(sessions, args, signals)
        elif args.mode == "publish-portfolio":
            await _portfolio(sessions, args, signals)
        elif args.mode in {"hold-fact", "fail-fact", "project-facts", "repair-fact"}:
            await _facts(sessions, args, signals)
        else:
            await _memory(sessions, args, signals)
        signals.emit("done", mode=args.mode)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("loop_id")
    parser.add_argument("directory")
    parser.add_argument("--entity-id")
    asyncio.run(_main(parser.parse_args()))
