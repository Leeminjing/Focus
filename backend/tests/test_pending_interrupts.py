"""未决人工中断的投影与主运行阻塞粒度用例。

输入为检查点、桌面线程与运行状态；输出为未决中断的类型、载荷与收敛状态。
工作流先锁定按类型取载荷与状态归一，再锁定投影只读主执行命名空间——后台执行主体的
待决位于各自命名空间，因此不会阻塞主运行，最后在服务入口断言主执行存在待决时新运行被拒。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from focus.security.approval import APPROVAL_TYPE

from backend.app.desktop.pending_interrupts import (
    MAIN_CHECKPOINT_NAMESPACE,
    main_pending_interrupt,
    main_pending_kinds,
    main_run_status,
    pending_interrupt_payload,
)

_OTHER_TYPE = "compression_request"
_ACCESS = {"type": APPROVAL_TYPE, "tool": "bash", "cwd": "C:/ws"}


class _Checkpoint:
    def __init__(self, *payloads):
        self.pending_writes = [("task", "__interrupt__", payload) for payload in payloads]


class _Checkpointer:
    """按命名空间返回检查点，并记录被询问过的命名空间。"""

    def __init__(self, by_namespace):
        self._by_namespace = by_namespace
        self.asked: list[str] = []

    async def aget_tuple(self, config):
        namespace = config["configurable"].get("checkpoint_ns", "")
        self.asked.append(namespace)
        return self._by_namespace.get(namespace)


class _Session:
    def __init__(self, run=None):
        self._run = run

    async def scalar(self, _query):
        return self._run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _task():
    return SimpleNamespace(thread_id="thread-1", task_id="task-1")


def test_payload_is_selected_by_type():
    checkpoint = _Checkpoint(_ACCESS, {"type": _OTHER_TYPE})
    assert pending_interrupt_payload(checkpoint, APPROVAL_TYPE) == _ACCESS
    assert pending_interrupt_payload(checkpoint, _OTHER_TYPE) == {"type": _OTHER_TYPE}
    assert pending_interrupt_payload(checkpoint, "unknown_type") is None


def test_latest_matching_payload_wins():
    older = {"type": APPROVAL_TYPE, "tool": "older"}
    newer = {"type": APPROVAL_TYPE, "tool": "newer"}
    checkpoint = _Checkpoint(older, newer)
    assert pending_interrupt_payload(checkpoint, APPROVAL_TYPE)["tool"] == "newer"


def test_missing_checkpoint_is_no_pending():
    checkpointer = _Checkpointer({})
    assert asyncio.run(main_pending_interrupt(None, _task(), checkpointer, APPROVAL_TYPE)) is None
    assert asyncio.run(main_pending_kinds(None, _task(), checkpointer, (APPROVAL_TYPE,))) == []


def test_projection_only_reads_main_namespace():
    """投影只读主执行命名空间；后台执行主体的待决不在其中。"""
    checkpointer = _Checkpointer(
        {MAIN_CHECKPOINT_NAMESPACE: _Checkpoint(), "swarm:agent-1": _Checkpoint(_ACCESS)}
    )
    pending = asyncio.run(main_pending_interrupt(_Session(), _task(), checkpointer, APPROVAL_TYPE))
    assert pending is None
    assert checkpointer.asked == [MAIN_CHECKPOINT_NAMESPACE]


def test_background_pending_does_not_block_main_run():
    """后台执行主体存在未决时，主执行的未决集合仍为空。"""
    checkpointer = _Checkpointer(
        {MAIN_CHECKPOINT_NAMESPACE: _Checkpoint(), "patrol:spatial-1": _Checkpoint(_ACCESS)}
    )
    kinds = asyncio.run(
        main_pending_kinds(_Session(), _task(), checkpointer, (_OTHER_TYPE, APPROVAL_TYPE))
    )
    assert kinds == []


def test_main_pending_interrupt_reports_type_and_status():
    checkpointer = _Checkpointer({MAIN_CHECKPOINT_NAMESPACE: _Checkpoint(_ACCESS)})
    session = _Session(SimpleNamespace(status="interrupted"))
    pending = asyncio.run(main_pending_interrupt(session, _task(), checkpointer, APPROVAL_TYPE))
    assert pending["status"] == "resumable"
    assert pending["request"]["type"] == APPROVAL_TYPE


def test_main_pending_kinds_preserves_requested_order():
    checkpointer = _Checkpointer({MAIN_CHECKPOINT_NAMESPACE: _Checkpoint(_ACCESS)})
    kinds = asyncio.run(
        main_pending_kinds(_Session(), _task(), checkpointer, (_OTHER_TYPE, APPROVAL_TYPE))
    )
    assert kinds == [APPROVAL_TYPE]


def test_run_status_normalisation():
    for status, expected in (
        ("pending", "processing"),
        ("running", "processing"),
        ("interrupted", "resumable"),
        ("success", "orphaned"),
        ("error", "orphaned"),
    ):
        session = _Session(SimpleNamespace(status=status))
        assert asyncio.run(main_run_status(session, _task())) == expected
    assert asyncio.run(main_run_status(_Session(None), _task())) == "orphaned"


async def _stub_task_entities(_session, task_id):
    workspace = SimpleNamespace(path="C:/ws")
    return SimpleNamespace(task_id=task_id, workspace_id="w-1", thread_id="thread-1"), workspace


async def _stub_ensure_runnable(_session, _task_id):
    return None


async def _stub_no_commitment(_session, _task):
    return None


async def _stub_no_compression(_session, _task, _checkpointer):
    return None


async def _stub_pending_access(_session, _task, _checkpointer, _payload_type):
    return {"status": "resumable", "request": _ACCESS}


def _stub_service(monkeypatch):
    import backend.app.desktop.service as service_module

    service = service_module.DesktopService.__new__(service_module.DesktopService)
    service.session_factory = _Session
    service.checkpointer = None
    service._get_task_entities = _stub_task_entities
    service._commitment_recovery_payload = _stub_no_commitment
    service.contexts = SimpleNamespace(ensure_runnable=_stub_ensure_runnable)
    monkeypatch.setattr(service_module, "compression_recovery_payload", _stub_no_compression)
    monkeypatch.setattr(service_module, "main_pending_interrupt", _stub_pending_access)
    return service


def test_pending_access_blocks_new_main_run(monkeypatch):
    """主执行存在未决准入时，新主运行被拒并给出可识别的错误码。"""
    service = _stub_service(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(service.start_main_run("task-1", "你好", None, ["read"], []))
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "access_review_pending"


def test_no_pending_access_lets_new_main_run_proceed(monkeypatch):
    """无未决准入时不因准入门拦截（后续装配失败属既有装配路径，与本用例无关）。"""
    import backend.app.desktop.service as service_module

    async def _no_pending(_session, _task, _checkpointer, _payload_type):
        return None

    service = _stub_service(monkeypatch)
    monkeypatch.setattr(service_module, "main_pending_interrupt", _no_pending)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(service.start_main_run("task-1", "你好", None, ["read"], []))
    message = str(getattr(excinfo.value, "detail", excinfo.value))
    assert "access_review_pending" not in message
