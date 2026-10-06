"""本文件对外提供 DEFAULT_SETTINGS 接口错误的真实 TypeScript 构建及执行证据回归。

输入为固定 TypeScript 5.9.3、隔离项目和生产 Shell 工具；输出为真实失败退出码、修复后构建与运行成功。
具体工作流为复制配置工厂、引入不存在的旧导出，经实际 PowerShell 运行编译器，再修复调用方并重新构建测试；真实 Node TAP 同时证明命名结果来自执行。
示例：pytest backend/tests/test_loop_settings_build_evidence.py；只使用 Focus 内的测试 fixture。
"""

import json
from pathlib import Path
import shutil

from backend.tests.runtime_context_support import tool_runtime
from backend.app.desktop.domain_evidence.tests import TestResultParser
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import powershell


FIXTURE = Path(__file__).parent / "fixtures" / "settings-contract"


def _execute(project, command, call_id):
    runtime = tool_runtime(agent_id="main:settings", task_id="settings", workspace=str(project),
                           permissions=("read", "write", "host_command"), access_mode=AccessMode.FULL)
    runtime.tool_call_id = call_id
    content = powershell.func(command=command, runtime=runtime)
    fact = json.loads(content)
    assert fact["status"] == "exited"
    return fact, TestResultParser.parse("powershell", content, command=command, run_id="run-1", call_id=call_id)


def test_default_settings_export_failure_blocks_build_until_consumer_is_repaired(tmp_path):
    project = tmp_path / "settings-contract"
    shutil.copytree(FIXTURE / "src", project / "src")
    shutil.copyfile(FIXTURE / "tsconfig.json", project / "tsconfig.json")
    consumer = project / "src" / "settings.test.ts"
    consumer.write_text('import { DEFAULT_SETTINGS } from "./settings";\nconsole.log(DEFAULT_SETTINGS);\n', encoding="utf-8")
    compiler = FIXTURE / "node_modules" / "typescript" / "bin" / "tsc"
    assert compiler.is_file(), "请先 npm ci --offline 安装固定 TypeScript fixture"
    command = f"& node '{compiler}' --project tsconfig.json; exit $LASTEXITCODE"
    failed, evidence = _execute(project, command, "build-red")
    assert failed["exit_code"] != 0 and "TS2305" in failed["output"] and "DEFAULT_SETTINGS" in failed["output"]
    assert evidence["status"] == "failed" and evidence["execution_proof"]["bound"]
    assert not (project / "dist" / "settings.test.js").exists()
    shutil.copyfile(FIXTURE / "src" / "settings.test.ts", consumer)
    passed, evidence = _execute(project, command, "build-green")
    assert passed["exit_code"] == 0 and evidence["status"] == "verified"
    tested, evidence = _execute(project, "& node --test --test-reporter=tap dist/settings.test.js; exit $LASTEXITCODE", "test-green")
    assert tested["exit_code"] == 0 and "# fail 0" in tested["output"]
    assert evidence["status"] == "verified" and evidence["metrics"]["passed"] == 1
    assert evidence["named_results"]["status"] == "complete"
    assert len(evidence["named_results"]["cases"]) == 1
    assert evidence["named_results"]["cases"][0]["status"] == "passed"
