"""本文件对外提供 record_verified_fixture 的真实命令验收来源及 claim_verifier 的生产领取身份。

输入为隔离数据库、Loop/Context/Round 与临时项目目录；输出为真实 pytest 退出码对应的不可变领域 source_id。
具体工作流为生成独立默认配置工厂及隔离状态测试，执行真实命令，持久化已结算 Run 与命令证明；Verifier 经真实 WorkerRuntime 领取后返回身份。
本夹具只验证 Kernel 的证据准入，不代表完整 Obsidian 插件已经交付。
示例：source = await record_verified_fixture(sessions, loop_id, context_id, round_id, project, revision)。
"""

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from backend.app.desktop.models import DesktopRun
from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.app.desktop.domain_evidence.repository import DomainResultRepository


async def claim_verifier(sessions, loop_id, worker_id):
    from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime

    claimed = await LoopWorkerRuntime(sessions, None)._claim_many(32, loop_id)
    return next(row.retry_identity for row in claimed if row.worker_request_id == worker_id)


async def record_verified_fixture(sessions, loop_id, context_id, round_id, project, revision):
    project = Path(project)
    fixture = project / "test_settings_isolation.py"
    fixture.write_text('''def create_defaults():
    return {"system_prompt": "pet", "messages": []}

def test_default_settings_do_not_share_session_state():
    first, second = create_defaults(), create_defaults()
    first["messages"].append("hello")
    assert second["messages"] == []
    assert first["system_prompt"] == second["system_prompt"]
''', encoding="utf-8")
    executed = subprocess.run([sys.executable, "-m", "pytest", str(fixture), "--noconftest", "-q", "-p", "no:cacheprovider"],
                              cwd=project, capture_output=True, text=True, timeout=30)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    run_id, call_id = uuid.uuid4().hex, uuid.uuid4().hex
    execution = json.dumps({"run_id": run_id, "call_id": call_id, "workspace": str(project),
                            "status": "exited", "exit_code": executed.returncode, "output": executed.stdout})
    parsed = TestResultParser.parse("powershell", execution, command="python -m pytest test_settings_isolation.py",
                                    run_id=run_id, call_id=call_id)
    async with sessions.begin() as session:
        session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id="fixture:" + run_id, kind="worker",
            status="success", loop_id=loop_id, round_id=round_id,
            equipment={"permissions": ["read", "host_command"], "access_mode": "read-only"},
            workspace_result={"revision": revision, "workspace_status": "settled"}, settled_at=datetime.now(UTC)))
        await session.flush()
        return await DomainResultRepository().record(session, loop_id=loop_id, context_id=context_id, run_id=run_id,
            kind="test", source_id=call_id, payload={**parsed, "workspace_revision": revision})
