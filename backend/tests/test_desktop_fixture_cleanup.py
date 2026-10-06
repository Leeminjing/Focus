"""本文件对外提供桌面集成夹具的异步清理屏障回归。
输入为受控且仍running的目标Run同步任务与另一个线程的任务；输出为取消只作用于目标、真实同步完成后才删除checkpoint和数据库的断言。
具体工作流为冻结同步任务在事件屏障上，通过现有_cleanup入口执行收口，再核对异步顺序；不使用等待秒数或删除真实项目。
示例：pytest backend/tests/test_desktop_fixture_cleanup.py；服务和存储端口为受控替身，原集成数据库断言仍由原用例执行。
"""
import asyncio
from types import SimpleNamespace

from backend.tests.test_desktop_poc import _cleanup


def test_cleanup_settles_running_target_before_delete_without_cancelling_other_thread():
    async def run():
        release = asyncio.Event()
        started = asyncio.Event()
        finished = asyncio.Event()
        untouched = asyncio.Event()
        cancelled = []

        async def sync_record(record):
            if record.thread_id == "target":
                started.set()
                await release.wait()
                finished.set()
            else:
                await untouched.wait()

        def cancel(identity):
            cancelled.append(identity)
            release.set()

        async def delete_checkpoint(identity):
            assert finished.is_set(), "运行中的同步未收口，不得删除checkpoint或数据库行"

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def execute(self, statement):
                assert finished.is_set()

            async def commit(self):
                assert finished.is_set()

        target = SimpleNamespace(thread_id="target", run_id="target-run", status=SimpleNamespace(value="running"))
        other = SimpleNamespace(thread_id="other", run_id="other-run", status=SimpleNamespace(value="running"))
        tasks = {asyncio.create_task(sync_record(target)), asyncio.create_task(sync_record(other))}
        service = SimpleNamespace(_sync_tasks=tasks, run_manager=SimpleNamespace(cancel=cancel),
            checkpointer=SimpleNamespace(adelete_thread=delete_checkpoint), session_factory=Session)
        await started.wait()
        try:
            await _cleanup(service, "target-task", "target-workspace", "target")
            assert finished.is_set() and cancelled == ["target-run"]
            assert sum(not task.done() for task in tasks) == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())
