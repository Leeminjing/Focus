r"""本文件对外提供索引进程中断与段缓存恢复的 PostgreSQL 回归。

输入为真实隔离数据库、冻结输入和独立子进程；输出为强杀提交前不泄漏、提交后零调用恢复的断言。
具体工作流为通过真实 Observation 服务冻结调用所有权，再只在测试目录启动隐藏子进程，等待信号后强杀，另一个新进程重放并复用独立段缓存。
模型消费按逐请求回执结算；进程恢复只补齐未发布结果，已完成段缓存跨进程复用。
示例：python -m pytest backend/tests/test_index_restart.py -q。
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.patrol.agent_loop.models import LoopBudgetUsage
from backend.tests.index_test_support import exercise, messages, observation, revision

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def process(directory, mode, obs):
    directory.mkdir()
    input_path = directory / "input.json"
    input_path.write_text(json.dumps(vars(obs)), encoding="utf-8")
    output = (directory / "process.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.tests.index_restart_process",
            mode,
            str(input_path),
            str(directory),
        ],
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return child, output


async def signal(directory, name, child):
    deadline = time.monotonic() + 30
    path = directory / f"{name}.json"
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if child.poll() is not None:
            raise AssertionError(
                (directory / "process.log").read_text(encoding="utf-8")
            )
        await asyncio.sleep(0.03)
    raise AssertionError(f"process signal timeout: {directory}")


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_independent_process_kill_and_persisted_segment_cache(tmp_path, boundary):
    async def run(sessions, seed):
        first = await revision(sessions, seed["context_id"], messages(12))
        directory = tmp_path / "killed"
        child, log = process(directory, boundary, observation(seed, first))
        try:
            state = await signal(
                directory, "prepared" if boundary == "before_commit" else "done", child
            )
            assert state["calls"] == 3
        finally:
            child.kill()
            await asyncio.to_thread(child.wait, 5)
            log.close()
        async with sessions() as session:
            stored = await session.scalar(
                select(LoopSemanticIndexArtifact.index_id).where(
                    LoopSemanticIndexArtifact.revision_id == first.ref.revision_id
                )
            )
            assert bool(stored) == (boundary == "after_commit")
            pending = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"],
                        LoopIndexBudgetReservation.settled_at.is_(None),
                    )
                )
            ).all()
            # The hold is after all three requests completed, before artifact
            # publication. Their per-request receipts are already settled;
            # killing here must not turn known usage into an unknown reserve.
            assert pending == []
            assert (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls == 3
        directory = tmp_path / "restarted"
        child, log = process(directory, "build", observation(seed, first))
        try:
            replay = await signal(directory, "done", child)
            assert await asyncio.to_thread(child.wait, 5) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            log.close()
        assert replay["calls"] == (0 if boundary == "after_commit" else 1)
        if stored:
            assert replay["index_id"] == stored
        new = await revision(sessions, seed["context_id"], messages(13))
        directory = tmp_path / "descendant"
        child, log = process(directory, "build", observation(seed, new))
        try:
            descendant = await signal(directory, "done", child)
            assert await asyncio.to_thread(child.wait, 5) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            log.close()
        assert descendant["calls"] == 3
        assert descendant["source_token"] == new.ref.revision_id
        assert set(replay["record_ids"]).issubset(descendant["record_ids"])
        assert descendant["pid"] != replay["pid"] != state["pid"]

    exercise(tmp_path, run)
