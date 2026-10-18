"""不依赖 Shell 的有界文件/字面文本搜索，遵守分层忽略规则且不跟随目录链接。

输入为已准入的搜索根及工作区；输出包含相对路径、行列、上下文和不完整原因。
扫描条目、总读取量、单文件、时间与展示量均有限额，跳过项不会伪装成完整无匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from pathlib import Path, PurePosixPath
import time

from langchain_core.tools import ToolException
from pathspec import GitIgnoreSpec

from focus.tools.builtins.workspace_text import MAX_FILE_BYTES, MAX_OUTPUT_CHARS, text_lines

MAX_ENTRIES = 10000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 5
MAX_DEPTH = 64
IGNORE_NAMES = (".gitignore", ".ignore", ".rgignore")


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


@dataclass
class SearchScan:
    root: Path
    workspace: Path
    include_hidden: bool
    entries: int = 0
    bytes_read: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    limits: set[str] = field(default_factory=set)
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def expired(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.limits.add("time_limit")
            return True
        return False

    def read(self, path: Path, limit: int) -> bytes | None:
        try:
            if _linked(path) or not path.is_file():
                self.skip("linked_or_special")
                return None
            if path.stat().st_size > limit:
                self.skip("file_too_large")
                return None
            remaining = MAX_TOTAL_BYTES - self.bytes_read
            if remaining <= 0:
                self.limits.add("byte_limit")
                return None
            with path.open("rb") as stream:
                raw = stream.read(min(limit + 1, remaining + 1))
            self.bytes_read += len(raw)
            if len(raw) > remaining:
                self.limits.add("byte_limit")
                return None
            if len(raw) > limit:
                self.skip("file_too_large")
                return None
            return raw
        except OSError:
            self.skip("unreadable")
            return None

    def rules(self, directory: Path, inherited: tuple) -> tuple:
        rules = list(inherited)
        for name in IGNORE_NAMES:
            path = directory / name
            if not path.exists():
                continue
            raw = self.read(path, 65536)
            if raw is None:
                # 忽略文件不可读时不继续搜索这个树，避免泄露本应忽略的内容。
                raise ToolException(f"无法完整读取忽略规则: {path}")
            try:
                rules.append((directory, GitIgnoreSpec.from_lines(raw.decode("utf-8-sig").splitlines())))
            except (UnicodeError, ValueError) as error:
                raise ToolException(f"忽略规则无效: {path}") from error
        return tuple(rules)

    def files(self):
        if self.root.is_file():
            yield self.root
            return
        if not self.root.is_dir():
            raise ToolException(f"搜索路径不存在或不是文件/目录: {self.root}")
        inherited: tuple = ()
        if self.root.is_relative_to(self.workspace):
            for parent in reversed(self.root.parents):
                if parent == self.workspace or parent.is_relative_to(self.workspace):
                    inherited = self.rules(parent, inherited)
        pending = [(self.root, inherited, 0)]
        while pending:
            if self.expired():
                return
            directory, parents, depth = pending.pop()
            if _linked(directory):
                self.skip("linked_or_special")
                continue
            rules = self.rules(directory, parents)
            try:
                with os.scandir(directory) as iterator:
                    entries = []
                    for entry in iterator:
                        if self.expired():
                            return
                        self.entries += 1
                        if self.entries > MAX_ENTRIES:
                            self.limits.add("entry_limit")
                            break
                        entries.append(Path(entry.path))
                for path in sorted(entries):
                    if path.name == ".git" or (not self.include_hidden and path.name.startswith(".")):
                        continue
                    if _linked(path):
                        self.skip("linked_or_special")
                        continue
                    is_dir = path.is_dir()
                    ignored = False
                    for base, spec in rules:
                        relative = path.relative_to(base).as_posix() + ("/" if is_dir else "")
                        included = spec.check_file(relative).include
                        if included is not None:
                            ignored = included
                    if ignored:
                        continue
                    if is_dir:
                        if depth >= MAX_DEPTH:
                            self.limits.add("depth_limit")
                        else:
                            pending.append((path, rules, depth + 1))
                    elif path.is_file():
                        yield path
                if "entry_limit" in self.limits:
                    return
            except OSError:
                self.skip("unreadable")


def _matches_glob(path: str, pattern: str) -> bool:
    candidate = PurePosixPath(path)
    while True:
        if candidate.match(pattern):
            return True
        if not pattern.startswith("**/"):
            return False
        pattern = pattern[3:]
        if not pattern:
            return False


def search(
    root: Path, workspace: Path, *, pattern: str, query: str | None,
    max_results: int, context_lines: int = 0, case_sensitive: bool = True,
    include_hidden: bool = False,
) -> dict:
    if not 1 <= max_results <= 200 or not 0 <= context_lines <= 5:
        raise ToolException("max_results 必须在 1..200，context_lines 必须在 0..5")
    if not pattern or len(pattern) > 512 or (query is not None and not 1 <= len(query) <= 2048):
        raise ToolException("pattern 必须包含 1..512 个字符，query 必须包含 1..2048 个字符")
    if query is not None and ("\n" in query or "\r" in query):
        raise ToolException("query 必须为单行字面文本；多行内容请用 read_file_lines 定位")
    scan = SearchScan(root, workspace, include_hidden)
    results: list[dict] = []
    shown = 0
    needle = None if query is None else re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    for path in scan.files():
        if scan.expired() or "byte_limit" in scan.limits:
            break
        relative = path.relative_to(root).as_posix() if root.is_dir() else path.name
        if not _matches_glob(relative, pattern):
            continue
        if query is None:
            candidates = iter([{"path": relative}])
        else:
            raw = scan.read(path, MAX_FILE_BYTES)
            if raw is None:
                continue
            try:
                if b"\0" in raw:
                    scan.skip("binary")
                    continue
                lines = [line for line, _ in text_lines(raw.decode("utf-8-sig"))]
            except UnicodeError:
                scan.skip("non_utf8")
                continue
            candidates = _text_matches(relative, lines, needle, context_lines, scan)
        for candidate in candidates:
            # 预留结构开销；单行片段和上下文也分别有界。
            size = len(str(candidate))
            if len(results) >= max_results or shown + size > MAX_OUTPUT_CHARS:
                scan.limits.add("result_limit" if len(results) >= max_results else "output_limit")
                break
            results.append(candidate)
            shown += size
        if scan.limits & {"result_limit", "output_limit", "time_limit"}:
            break
    return {"path": str(root), "matches": results, "truncated": bool(scan.limits),
            "limit_reasons": sorted(scan.limits), "skipped": scan.skipped,
            "incomplete": bool(scan.limits or scan.skipped), "entries_scanned": scan.entries}


def _text_matches(relative, lines, needle, context_lines, scan):
    for index, line in enumerate(lines):
        if scan.expired():
            return
        match = needle.search(line)
        if match is None:
            continue
        column = match.start()
        context = []
        for number in range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)):
            start = max(0, column - 100) if number == index else 0
            fragment = lines[number][start:start + 500]
            context.append({"line": number + 1, "column": start + 1, "text": fragment,
                            "truncated": start > 0 or start + len(fragment) < len(lines[number])})
        yield {"path": relative, "line": index + 1, "column": column + 1, "context": context}
