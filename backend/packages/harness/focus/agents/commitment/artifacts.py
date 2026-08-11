"""
本文件对外提供知识文件、任务合同和最终 HumanMessage 内容的持久化函数。

输入:
    阶段六 knowledge 结果、阶段七合同结果、thread_id、知识文件相对路径及工作区根目录。

输出:
    list[str] — 已写入的 knowledge 文件相对路径。
    tuple[str, str] — 合同正文及 task-contract.md 相对路径。
    str — 包含 task_contract 和 theoretical foundation 标签的最终消息。

具体工作流:
    (1) 校验技术名、版本和 thread_id 的安全路径片段。
    (2) 将官方知识写入工作区 knowledge 目录。
    (3) 将合同写入工作区 requirements/{thread_id}/task-contract.md。
    (4) 读取已确认知识并组装交给 lead agent 的第一条 HumanMessage 内容。

示例:
    contract, path = _write_contract(thread_id, stage_seven_result, workspace_root)
"""

from pathlib import Path
from typing import Any

from focus.agents.commitment.stage_rules import _safe_segment, _slug_segment


def _write_knowledge(result: dict[str, Any], workspace_root: str) -> list[str]:
    files: list[str] = []
    root = Path(workspace_root)
    for item in result.get("knowledge", []):
        if not isinstance(item, dict):
            raise ValueError("knowledge 项必须是对象")
        technology = str(item.get("technology", "")).strip()
        technology_slug = _slug_segment(technology, "technology")
        version = _safe_segment(str(item.get("version", "")), "version")
        source = str(item.get("source_url", "")).strip()
        content = str(item.get("content", "")).strip()
        if not source.startswith("http") or not content:
            raise ValueError("knowledge 项必须包含官方 source_url 和 content")
        relative_path = Path("knowledge") / f"{technology_slug}-{version}.md"
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {technology} {version}\n\nSource: {source}\n\n{content}\n",
            encoding="utf-8",
        )
        files.append(relative_path.as_posix())
    if not files:
        raise ValueError("阶段6没有产生知识文件")
    return files


def _write_contract(
    thread_id: str,
    result: dict[str, Any],
    workspace_root: str,
) -> tuple[str, str]:
    safe_thread_id = _safe_segment(thread_id, "thread_id")
    contract = str(result.get("contract_markdown", "")).strip()
    if not contract:
        raise ValueError("合同内容为空")
    relative_path = Path("requirements") / safe_thread_id / "task-contract.md"
    path = Path(workspace_root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contract + "\n", encoding="utf-8")
    return contract, relative_path.as_posix()


def _build_final_message(
    contract: str,
    knowledge_files: list[str],
    workspace_root: str,
) -> str:
    sections = [f"<task_contract>\n{contract}\n</task_contract>"]
    for name in knowledge_files:
        content = (Path(workspace_root) / name).read_text(encoding="utf-8")
        sections.append(
            f'<theoretical foundation source="{name}">\n'
            f"{content}\n"
            "</theoretical foundation>"
        )
    return "\n\n".join(sections)
