"""本文件对外提供 TestResultParser 与 test_identity 的纯测试领域解释。

输入为可信工具完成结果、工具名称及可选已绑定执行命令；输出为领域结论或 None。
具体工作流为先确认测试执行，再提取最终计数摘要；无可靠统计时明确 unknown，重复摘要不累加。
模型声称或普通终端错误不是调用本解释器的可信执行来源。
示例：TestResultParser.parse("pytest", "41 passed, 2 failed") 返回 failed 及 exact 数量。
"""

from __future__ import annotations

import re

from backend.app.desktop.domain_evidence.identity import canonical_hash

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
    ) -> dict | None:
        named = str(tool_name or "").strip().lower() in _TOOLS
        framework = bool(
            re.search(
                r"=+\s*test session starts\s*=+|\bTest Suites:\s|\btest result:\s",
                content,
            )
        )
        if not named and not framework and not (command and _COMMAND.search(command)):
            return None
        lines = [line for line in content.splitlines() if _COUNT.search(line)]
        if not lines:
            return {
                "summary": "检测到测试执行，但输出不足以可靠统计数量",
                "metrics": {"count_status": "unknown"},
                "status": "unknown",
            }
        counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        matches = list(_COUNT.finditer(lines[-1]))
        kinds = [
            match.group("kind").lower().replace("error", "errors")
            if match.group("kind").lower() == "error"
            else match.group("kind").lower()
            for match in matches
        ]
        if len(kinds) != len(set(kinds)):
            return {
                "summary": "测试统计存在歧义，数量待确认",
                "metrics": {"count_status": "unknown"},
                "status": "unknown",
            }
        for match, kind in zip(matches, kinds, strict=True):
            counts[kind] = int(match.group("count"))
        failed = (
            counts["failed"]
            or counts["errors"]
            or execution_status in {"error", "failed", "cancelled"}
        )
        status = "failed" if failed else "verified" if counts["passed"] else "unknown"
        summary = f"{counts['passed']} passed · {counts['failed']} failed · {counts['skipped']} skipped"
        if counts["errors"]:
            summary += f" · {counts['errors']} errors"
        return {
            "summary": summary,
            "metrics": {**counts, "count_status": "exact"},
            "status": status,
        }
