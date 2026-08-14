"""backend/tests 共享 fixture（change 13）：轮询等待统一入口。"""

import time

import pytest


@pytest.fixture
def wait_until():
    """轮询等待条件成立；超时抛 AssertionError。"""

    def _wait(condition, timeout=20, interval=0.2, message=""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(interval)
        raise AssertionError(message or f"条件在 {timeout}s 内未成立")

    return _wait


def _memory_status(service, run_id):
    """运行中的状态（pending/running）只存在于内存 RunManager；DB 在 worker 结束才同步。"""
    record = service.run_manager.get(run_id)
    return record.status.value if record else None


@pytest.fixture
def wait_for_memory_status(wait_until):
    """等待 run 内存态收敛（RunManager 权威，DB 由 worker 结束后薄任务同步）。"""

    def _wait(client, service, run_id, statuses, timeout=20):
        def check():
            status = client.portal.call(_memory_status, service, run_id)
            return status in statuses

        wait_until(
            check, timeout=timeout,
            message=f"run {run_id} 未在 {timeout}s 内到达 {statuses}",
        )

    return _wait
