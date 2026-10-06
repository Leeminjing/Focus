"""本文件对外提供 TestResultParser 与 test_identity 的纯测试领域解释。

输入为可信工具完成结果、工具名称、已绑定执行命令和 Run/call 身份；输出为领域结论或 None。
具体工作流为核对结构化命令退出码与来源，再提取最终测试计数和可信报告的脱敏命名结果；无可靠统计或报告时明确 unknown，重复摘要不累加。
旧文本仍可产生展示统计，只有绑定的执行证明才能用于必要检查；模型自述不提供该证明。
完整 TAP 从真实叶用例统计，不要求可选的 Node 汇总诊断；缓冲式报告按计划和闭合树验证。截断输出或无效 TAP 不采用汇总诊断，数量保持 unknown；仍保留真实命令失败事实。
受保护的 _uncounted_result 统一缺少可靠统计时的输出，真实执行失败优先于报告 unknown。
示例：TestResultParser.parse("pytest", "41 passed, 2 failed") 返回 failed 及 exact 数量。
"""

from __future__ import annotations

import re
import json

from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.test_report import parse_named_test_report

_COUNT = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|errors?)\b", re.IGNORECASE
)
_COMMAND = re.compile(
    r"(?:^|[\s>])(?:pytest|python\s+-m\s+pytest|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|vitest|jest|mocha)(?:\s|$)",
    re.IGNORECASE,
)
_TOOLS = frozenset(
    {"pytest", "unittest", "go test", "cargo test", "vitest", "jest", "mocha", "test"}
)
_BUILD = re.compile(r"\b(?:tsc|npm\s+(?:ci|install|run\s+build)|pnpm\s+(?:install|build)|yarn\s+(?:install|build)|python\s+-m\s+build)\b", re.I)


def test_identity(run_id: str, execution_id: str) -> str:
    return canonical_hash(["test_execution", run_id, execution_id])


class TestResultParser:
    @classmethod
    def parse(
        cls,
        tool_name: str | None,
        content: str,
        *,
        command: str | None = None,
        execution_status: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> dict | None:
        try:
            execution = json.loads(content)
        except (ValueError, TypeError):
            execution = None
        proof = None
        if isinstance(execution, dict) and "exit_code" in execution:
            bound = (run_id is None or execution.get("run_id") == run_id) and (call_id is None or execution.get("call_id") == call_id)
            proof = {key: execution.get(key) for key in ("run_id", "call_id", "status", "exit_code", "workspace")}
            proof["bound"] = bound
            proof["command"] = command
            content = str(execution.get("output") or "")
            if command and _BUILD.search(command):
                return {"status": "verified" if bound and proof["status"] == "exited" and proof["exit_code"] == 0 else "failed",
                        "summary": "构建/依赖命令已结束", "execution_proof": proof, "metrics": {"count_status": "not_applicable"}}
        named = str(tool_name or "").strip().lower() in _TOOLS
        framework = bool(
            re.search(
                r"=+\s*test session starts\s*=+|\bTest Suites:\s|\btest result:\s|^TAP version \d+",
                content,
                re.MULTILINE,
            )
        )
        if not named and not framework and not (command and _COMMAND.search(command)):
            return None
        named_results = parse_named_test_report(content, trusted=bool(proof and proof["bound"] and proof["status"] == "exited"), secrets=secrets)
        if isinstance(execution, dict) and execution.get("truncated"):
            named_results = {**named_results, "status": "unknown", "cases": [], "suites": []}
        metadata = {"named_results": named_results, **({"execution_proof": proof} if proof is not None else {})}
        if (isinstance(execution, dict) and execution.get("truncated")) or (
            re.search(r"^TAP version \d+", content, re.MULTILINE)
            and named_results["status"] != "complete"
        ):
            return cls._uncounted_result(metadata, proof, execution_status, "测试报告截断或协议不完整，数量待确认")
        tap = dict(re.findall(r"^# (pass|fail|skipped|cancelled) (\d+)\s*$", content, re.MULTILINE))
        if tap:
            content += "\n" + " ".join(f"{tap.get(source, '0')} {target}" for source, target in
                                        (("pass", "passed"), ("fail", "failed"), ("skipped", "skipped"), ("cancelled", "errors")))
        elif named_results['status'] == 'complete':
            content += '\n' + ' '.join(f"{sum(c['status'] == state for c in named_results['cases'])} {label}"
                for state, label in (('passed', 'passed'), ('failed', 'failed'), ('skipped', 'skipped')))
        lines = [line for line in content.splitlines() if _COUNT.search(line)]
        if not lines:
            return cls._uncounted_result(metadata, proof, execution_status, "检测到测试执行，但输出不足以可靠统计数量")
        counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        matches = list(_COUNT.finditer(lines[-1]))
        kinds = [
            match.group("kind").lower().replace("error", "errors")
            if match.group("kind").lower() == "error"
            else match.group("kind").lower()
            for match in matches
        ]
        if len(kinds) != len(set(kinds)):
            return cls._uncounted_result(metadata, proof, execution_status, "测试统计存在歧义，数量待确认")
        for match, kind in zip(matches, kinds, strict=True):
            counts[kind] = int(match.group("count"))
        failed = (
            counts["failed"]
            or counts["errors"]
            or any(s['status'] == 'failed' for s in named_results.get('suites', ()))
            or execution_status in {"error", "failed", "cancelled"}
            or (proof is not None and (not proof["bound"] or proof["exit_code"] != 0 or proof["status"] != "exited"))
        )
        status = "failed" if failed else "verified" if counts["passed"] else "unknown"
        summary = f"{counts['passed']} passed · {counts['failed']} failed · {counts['skipped']} skipped"
        if counts["errors"]:
            summary += f" · {counts['errors']} errors"
        return {
            "summary": summary,
            "metrics": {**{key: value for key, value in counts.items() if key != "errors" or value}, "count_status": "exact"},
            "status": status,
            **metadata,
        }

    @staticmethod
    def _uncounted_result(metadata, proof, execution_status, summary):
        failed = execution_status in {"error", "failed", "cancelled"} or (
            proof is not None
            and (not proof["bound"] or proof["exit_code"] != 0 or proof["status"] != "exited")
        )
        return {"summary": summary, "metrics": {"count_status": "unknown"},
                "status": "failed" if failed else "unknown", **metadata}
