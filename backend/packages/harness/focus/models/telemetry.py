"""Content-free model invocation correlation and JSON performance records.

Request records count HTTP attempts (including SDK retries), not estimated usage.
Headers, URLs, prompts, responses and credential identities never enter telemetry.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger("focus.performance")
model_context = ContextVar("model_performance_context", default=None)
operation_context = ContextVar("performance_operation", default="other")


def emit(event, **fields):
    record = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
    logger.info("focus_metric %s", json.dumps(record, ensure_ascii=False, separators=(",", ":")))


@contextmanager
def model_invocation(*, category, **fields):
    parent = model_context.get() or {}
    context = {"invocation_id": parent.get("invocation_id", uuid4().hex),
               **parent, "category": category, **fields}
    token = model_context.set(context)
    try:
        yield context
    finally:
        model_context.reset(token)


def observe_operation(category):
    def decorate(function):
        @wraps(function)
        async def measured(*args, **kwargs):
            token = operation_context.set(category)
            started = perf_counter()
            try:
                return await function(*args, **kwargs)
            finally:
                emit("operation", category=category, duration_ms=(perf_counter() - started) * 1000)
                operation_context.reset(token)
        return measured
    return decorate


class RequestTiming:
    def __init__(self, request, identity=None):
        self.context = {**(identity or {}), **(model_context.get() or {})}
        self.context.setdefault("invocation_id", uuid4().hex)
        self.context.setdefault("category", "unclassified")
        self.started = perf_counter()
        self.headers_ms = None
        self.status = None
        self.closed = False
        try:
            self.retry = max(0, int(request.headers.get("x-stainless-retry-count", "0")))
        except (TypeError, ValueError):
            self.retry = 0

    def headers(self, response):
        self.status = response.status_code
        self.headers_ms = (perf_counter() - self.started) * 1000

    def finish(self, outcome, error=None):
        if self.closed:
            return
        self.closed = True
        emit("provider_request", **self.context, status_code=self.status,
             sdk_retry=self.retry, outcome=outcome, error_type=type(error).__name__ if error else None,
             headers_ms=self.headers_ms, duration_ms=(perf_counter() - self.started) * 1000)
