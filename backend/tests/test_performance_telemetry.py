"""Attempt accounting, cancellation, streaming lifetime and content-free metrics."""

import asyncio
import json
import logging

import httpx
from openai import AsyncOpenAI
import pytest

from focus.models.http_clients import ObservedAsyncHttpClient
from focus.models.telemetry import model_invocation
from backend.scripts.desktop_performance_report import summarize


def records(caplog):
    return [json.loads(record.getMessage().split("focus_metric ", 1)[1])
            for record in caplog.records if record.getMessage().startswith("focus_metric ")]


def test_sdk_retry_attempts_group_by_invocation_without_payload_or_credentials(caplog):
    async def run():
        calls = 0
        async def send(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": {"message": "private error"}})
            return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "private output"}, "finish_reason": "stop"}]})
        async with AsyncOpenAI(api_key="private-key", base_url="https://provider.invalid/v1", max_retries=1,
            http_client=ObservedAsyncHttpClient(transport=httpx.MockTransport(send),
                performance_identity={"provider": "test", "model": "test"})) as client:
            with model_invocation(category="run", run_id="test-run"):
                await client.chat.completions.create(model="test", messages=[{"role": "user", "content": "private prompt"}])
    with caplog.at_level(logging.INFO, logger="focus.performance"):
        asyncio.run(run())
    attempts = records(caplog)
    report = summarize(attempts)["provider_requests"][0]
    assert report["http_attempts"] == 2 and report["invocations"] == 1
    assert report["rate_429"] == 0.5 and report["invocation_retry_rate"] == 1
    assert [row["sdk_retry"] for row in attempts] == [0, 1]
    assert "private" not in json.dumps(attempts)
    assert "provider.invalid" not in json.dumps(attempts)


def test_stream_duration_includes_body_and_cancellation_is_reported_once(caplog):
    async def run():
        started = asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first"
                started.set()
                await asyncio.Event().wait()
        async def send(_request):
            return httpx.Response(200, stream=Body())
        async with ObservedAsyncHttpClient(transport=httpx.MockTransport(send)) as client:
            response = await client.send(client.build_request("GET", "https://test.invalid"), stream=True)
            assert records(caplog) == []
            task = asyncio.create_task(response.aread())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await response.aclose()
    with caplog.at_level(logging.INFO, logger="focus.performance"):
        asyncio.run(run())
    assert len(records(caplog)) == 1
    row = records(caplog)[0]
    assert row["outcome"] == "cancelled"
    assert row["duration_ms"] >= row["headers_ms"] >= 0


def test_missing_metrics_remain_unknown():
    assert summarize([])["queues"] == {}
    assert summarize([])["material_scans"] is None
    assert summarize([{"event": "process_sample", "process": "backend", "pid": 1,
                       "rss_bytes": 100, "cpu_percent": 0}])["processes"][0]["rss_delta_bytes"] is None
    report = summarize([{"event": "run_dispatch", "category": "run", "queue_wait_ms": 10}])
    assert report["queues"]["run"]["execution_ms"]["p95"] is None


def test_nonstream_header_timing_precedes_slow_body(caplog):
    async def run():
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(0.04)
                yield b"body"
        async def send(_request):
            return httpx.Response(200, stream=Body())
        async with ObservedAsyncHttpClient(transport=httpx.MockTransport(send)) as client:
            assert (await client.get("https://test.invalid")).content == b"body"
    with caplog.at_level(logging.INFO, logger="focus.performance"):
        asyncio.run(run())
    row = records(caplog)[0]
    assert row["duration_ms"] - row["headers_ms"] >= 30


def test_index_cancel_timing_includes_model_cleanup_and_queued_waiter_release(caplog):
    from types import SimpleNamespace
    from backend.app.desktop.agent_loop.context_expansion.index_model_budget import BudgetedIndexModel
    from backend.app.desktop.agent_loop.context_expansion.index_scheduling import IndexModelSlots

    async def run():
        started = asyncio.Event()
        class Model:
            async def invoke(self, *_args):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.04)
        class Budget:
            usage_receipts = None
            async def admit(self, *_args, **_kwargs):
                return None
        slots = IndexModelSlots(1)
        model = BudgetedIndexModel(Model(), Budget(), semaphore=slots)
        task = asyncio.create_task(model.invoke(SimpleNamespace(__name__="Test"), "", {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slots._busy == slots._foreground_waiters == 0
        async with slots.slot():
            queued = asyncio.create_task(model.invoke(SimpleNamespace(__name__="Test"), "", {}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert slots._foreground_waiters == 0
    with caplog.at_level(logging.INFO, logger="focus.performance"):
        asyncio.run(run())
    cancellations = [row for row in records(caplog) if row["event"] == "index_cancelled"]
    assert len(cancellations) == 2
    assert cancellations[0]["queued"] is False and cancellations[0]["release_ms"] >= 30
    assert cancellations[1]["queued"] is True


@pytest.mark.usefixtures("isolated_postgres_database")
def test_process_monitor_counts_sql_errors_and_releases_hooks_idempotently(caplog):
    import os
    from sqlalchemy import event, text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine
    from backend.app.desktop.performance import DesktopPerformanceMonitor

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        monitor = DesktopPerformanceMonitor(engine)
        monitor.start()
        monitor.start()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                with pytest.raises(DBAPIError):
                    await connection.execute(text("SELECT missing_column_for_performance_test"))
                await connection.rollback()
            await monitor.sample()
        finally:
            await monitor.close()
            await monitor.close()
            assert not event.contains(engine.sync_engine, "before_cursor_execute", monitor._before)
            await engine.dispose()
    with caplog.at_level(logging.INFO, logger="focus.performance"):
        asyncio.run(run())
    samples = [row for row in records(caplog) if row["event"] == "process_sample"]
    assert samples[0]["sql"]["other"]["queries"] >= 2
    assert samples[0]["sql"]["other"]["errors"] == 1
    assert "missing_column_for_performance_test" not in json.dumps(samples)
