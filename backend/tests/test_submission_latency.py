"""Submission latency must retain incomplete samples and exclude empty output."""

import asyncio

from focus.runtime.stream_bridge.schemas import StreamEvent

from backend.app.desktop.agent_loop.run_activity_bridge import LoopRunActivityBridge
from backend.app.desktop.agent_loop.submission_latency import (
    MILESTONES, SubmissionSample, measure_submission, summarize,
)


def test_incomplete_samples_do_not_become_fast_successes():
    samples = []
    for repeat, duration in enumerate((10, 30, 20)):
        sample = SubmissionSample("cold", repeat)
        sample.observe("durable_acceptance", duration, {})
        if repeat != 1:
            sample.observe("first_effective_output", duration * 100, {})
            sample.outcome = "complete"
        samples.append(sample)
    report = summarize(samples)["scenarios"]["cold"]
    assert report["samples"] == 3 and report["completed"] == 2
    assert report["metrics"]["durable_acceptance"]["p50_ms"] == 20
    output = report["metrics"]["first_effective_output"]
    assert output == {"observed": 2, "missing": 1, "p50_ms": 1000, "p95_ms": 2000,
                      "p95_sample_sufficient": False}
    assert summarize(samples)["scenarios"]["exact_cache"]["metrics"]["first_run_started"]["p95_ms"] is None


def test_observer_includes_submission_and_keeps_first_seen_milestone():
    async def run():
        receipt = {key: key for key in ("submission_id", "loop_id", "intent_id", "observation_id", "round_id")}

        async def submit():
            await asyncio.sleep(.01)
            return receipt

        class Probe:
            count = 0

            async def read(self, accepted):
                assert accepted == receipt
                self.count += 1
                names = MILESTONES[1:] if self.count == 2 else MILESTONES[1:2]
                return {name: {"decision_id": "d"} for name in names}, [], {"status": "running"}

        sample = await measure_submission(submit, Probe(), scenario="cold", repeat=0, poll_seconds=.01)
        assert sample.outcome == "complete"
        times = sample.milestones_ms
        assert times["durable_acceptance"] >= 10
        assert times["durable_acceptance"] <= times["patrol_decision_committed"] < times["first_run_started"]

    asyncio.run(run())


def test_timeout_bounds_submission_and_probe_and_retains_partial_data():
    async def run():
        async def slow_submit():
            await asyncio.sleep(10)

        sample = await measure_submission(slow_submit, None, scenario="cold", repeat=0, timeout_seconds=.01)
        assert sample.outcome == "timeout" and sample.milestones_ms == {}

        async def submit():
            return {key: key for key in ("submission_id", "loop_id", "intent_id", "observation_id", "round_id")}

        class Probe:
            async def read(self, receipt):
                await asyncio.sleep(10)

        sample = await measure_submission(submit, Probe(), scenario="cold", repeat=0, timeout_seconds=.01)
        assert sample.outcome == "timeout" and list(sample.milestones_ms) == ["durable_acceptance"]

    asyncio.run(run())


def test_effective_output_excludes_history_empty_and_reasoning_and_is_once_per_run():
    bridge = object.__new__(LoopRunActivityBridge)
    bridge._anchor_message_id, bridge._run_id, bridge._context_id = "anchor", "run", "context"
    bridge._correlation_id, bridge._seen, bridge._calls = "decision", set(), {}

    def event(messages):
        return StreamEvent(id="", event="events", data={"data": {"messages": messages}})

    old = {"role": "ai", "id": "old", "content": "old output"}
    anchor = {"role": "human", "id": "anchor", "content": "request"}
    empty = {"role": "ai", "id": "new", "content": "", "reasoning_content": "private"}
    assert bridge._drafts(event([old])) == ()
    assert all(d.kind != "context.output.available" for d in bridge._drafts(event([old, anchor, empty])))
    actual = {**empty, "content": [{"type": "text", "text": "  result  "}]}
    drafts = bridge._drafts(event([old, anchor, actual]))
    output = next(d for d in drafts if d.kind == "context.output.available")
    assert output.payload["output_kind"] == "text" and "result" not in str(output.payload)
    assert bridge._drafts(event([old, anchor, actual])) == ()


def test_tool_call_is_effective_output_without_text():
    bridge = object.__new__(LoopRunActivityBridge)
    bridge._run_id, bridge._context_id, bridge._correlation_id, bridge._seen = "r", "c", "d", set()
    assert bridge._effective_output({"id": "a", "content": " ", "tool_calls": [{"id": "call"}]}) is None
    output = bridge._effective_output({"id": "a", "tool_calls": [{"id": "call", "name": "read_file"}]})
    assert output.payload["output_kind"] == "tool_call"


def test_host_suspension_is_not_reported_as_a_normal_latency_sample(monkeypatch):
    from backend.app.desktop.agent_loop import submission_latency

    ticks = iter((0, .001, 12, 12))
    monkeypatch.setattr(submission_latency, "perf_counter", lambda: next(ticks))

    async def submit():
        return {key: key for key in ("submission_id", "loop_id", "intent_id", "observation_id", "round_id")}

    class Probe:
        async def read(self, receipt):
            return {name: {} for name in MILESTONES[1:]}, [], {"status": "running"}

    sample = asyncio.run(measure_submission(submit, Probe(), scenario="cold", repeat=0, timeout_seconds=1))
    assert sample.outcome == "observer_interrupted"
    assert sample.failure_code == "observation_deadline_exceeded"
    assert list(sample.milestones_ms) == ["durable_acceptance"]
    assert set(sample.discarded_milestones_ms) == set(MILESTONES[1:])
    assert summarize([sample])["scenarios"]["cold"]["metrics"]["first_effective_output"]["observed"] == 0


def test_primary_output_does_not_imply_a_derived_context_is_available():
    async def submit():
        return {key: key for key in ("submission_id", "loop_id", "intent_id", "observation_id", "round_id")}

    class Probe:
        async def read(self, receipt):
            return {name: {} for name in MILESTONES[1:] if name != "derived_context_available"}, [], {"status": "running"}

    sample = asyncio.run(measure_submission(submit, Probe(), scenario="cold", repeat=0, timeout_seconds=.02, poll_seconds=.01))
    assert sample.outcome == "timeout"
    assert "first_effective_output" in sample.milestones_ms
    assert "derived_context_available" not in sample.milestones_ms
