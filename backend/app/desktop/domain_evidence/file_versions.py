"""本文件提供 FileVersionCapture 和 FileVersionRecorder，采集并发布不可变文件版本。

输入为 Run 的持久 slot、已授权工具边界与秘密值；输出为有界脱敏前后快照及 Artifact/outbox 引用。
工作流在写入前后读取真实字节；Shell 仅比较 Git 已知路径和执行后新增的非忽略路径，清理未确认不发布。
完整字节流计算 hash，正文每侧最多保留 1 MiB，原始 hash 与展示 hash 分离；缓存重放不采集，未知前态不当作空文件。
重要性只取显式产物路径和已受理用户引用，不从自然语言猜测，也不提升证据可信度。
示例：capture.before_write(path); capture.after_write(path); await recorder.record(session, run, attempt, capture)。
"""

from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from sqlalchemy import select

from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.run_orchestration.outbox import RunOutboxRepository
from backend.app.desktop.secret_redaction import redact_text
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal


class FileVersionCapture:
    def __init__(self, slot_id, root, secrets=()):
        self.slot_id = slot_id
        self.root = Path(root).resolve()
        self._secrets = secrets
        self._before = {}
        self._shell_observed = False
        self.changes = []

    def _path(self, path):
        target = Path(path)
        target = (self.root / target).resolve() if not target.is_absolute() else target.resolve()
        return target if target.is_relative_to(self.root) else None

    def _snapshot(self, path):
        target = self._path(path)
        if target is None:
            return {"available": False, "reason": "outside_slot"}
        try:
            with target.open("rb") as stream:
                initial = os.fstat(stream.fileno())
                digest, preview, size = sha256(), bytearray(), 0
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    size += len(chunk)
                    if size <= 1048576:
                        preview.extend(chunk)
                final = target.stat()
                if (initial.st_ino, initial.st_size, initial.st_mtime_ns) != (final.st_ino, final.st_size, final.st_mtime_ns):
                    return {"available": False, "reason": "changed_during_capture"}
            base = {"exists": True, "sha256": digest.hexdigest(), "size_bytes": size}
            if size > 1048576:
                return {**base, "available": False, "reason": "over_1_mib"}
            if b"\x00" in preview:
                return {**base, "available": False, "reason": "binary"}
            try:
                text = bytes(preview).decode("utf-8")
            except UnicodeDecodeError:
                return {**base, "available": False, "reason": "non_utf8"}
            safe = redact_text(text, self._secrets)
            safe = re.sub(r'(?im)((?:api[_-]?key|token|secret|password|credential)[\w-]*["\x27]?\s*[:=]\s*)[^\r\n]+', r'\1***', safe)
            return {**base, "available": True, "text": safe, "display_hash": sha256(safe.encode()).hexdigest(), "redacted": safe != text}
        except FileNotFoundError:
            return {"exists": False, "available": True, "text": "", "sha256": None, "display_hash": sha256(b"").hexdigest()}
        except OSError:
            return {"available": False, "reason": "unreadable"}

    def before_write(self, path):
        target = self._path(path)
        if target is not None:
            self._before[target] = self._snapshot(target)

    def after_write(self, path, expected_content=None):
        target = self._path(path)
        if target is None:
            return
        before = self._before.get(target, {"available": False, "reason": "before_unobserved"})
        after = self._snapshot(target)
        if expected_content is not None:
            expected_bytes = (expected_content if isinstance(expected_content, bytes)
                              else expected_content.replace("\n", os.linesep).encode("utf-8"))
            if after.get("sha256") != sha256(expected_bytes).hexdigest():
                return
        if "sha256" in before and "sha256" in after and before["sha256"] == after["sha256"]:
            return
        if "exists" not in after:
            return
        operation = "deleted" if not after["exists"] else "created" if before.get("exists") is False else "modified"
        self.changes.append({"slot_id": self.slot_id, "path": target.relative_to(self.root).as_posix(),
            "operation": operation, "before": before, "after": after})

    def _git_paths(self):
        try:
            result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=self.root, capture_output=True, timeout=10, check=True)
            return {self.root / path.decode("utf-8") for path in result.stdout.split(b"\0") if path}
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None

    def before_shell(self, workspace):
        if Path(workspace).resolve() != self.root:
            return
        paths = self._git_paths()
        if paths is not None:
            self._shell_observed = True
            targets = {target for path in paths if (target := self._path(path)) is not None}
            self._before = {target: self._snapshot(target) for target in targets}

    def after_shell(self, cleanup):
        if cleanup != "confirmed" or not self._shell_observed:
            return
        paths = self._git_paths()
        if paths is None:
            return
        for path in set(self._before) | paths:
            self.after_write(path)


class FileVersionRecorder:
    async def record(self, session, run, attempt, capture):
        if not capture.changes:
            return
        from backend.app.desktop.agent_loop.models import LoopUserIntent

        requests = await session.scalars(select(LoopUserIntent.request_payload["request"]["file_refs"]).where(LoopUserIntent.loop_id == run.loop_id)) if run.loop_id else ()
        referenced = {(item.get("slot_id"), item.get("path")) for references in requests for item in (references or ())}
        declared = set()
        for key in ("artifacts", "artifact_paths", "output_files"):
            for item in (run.workspace_result or {}).get(key, ()):
                path = item if isinstance(item, str) else item.get("path") or item.get("name") if isinstance(item, dict) else None
                if path:
                    declared.add((capture.root / path).resolve())
        for change in capture.changes:
            importance = "user_reference" if (change["slot_id"], change["path"]) in referenced else "declared_artifact" if (capture.root / change["path"]).resolve() in declared else None
            payload = {"format": "file_version_v1", **change, "run_id": run.run_id,
                "importance": importance,
                "context_id": run.task_id, "round_id": run.round_id,
                "attempt_id": attempt.attempt_id, "call_id": attempt.call_id,
                "sha256": change["after"].get("sha256"), "size_bytes": change["after"].get("size_bytes")}
            key = await DomainResultRepository().record(session, kind="artifact",
                source_id=canonical_hash([attempt.attempt_id, change["slot_id"], change["path"]]),
                payload=payload, loop_id=run.loop_id, context_id=run.task_id, run_id=run.run_id,
                audit={"tool_attempt_id": attempt.attempt_id})
            if run.loop_id:
                await RunOutboxRepository().enqueue_artifact(session, run.run_id, key)

    async def publish(self, session, event):
        source = await session.get(DesktopDomainResult, event.payload.get("result_key"))
        if source is None or not source.loop_id or source.kind != "artifact":
            return
        payload = source.payload
        file = {key: payload.get(key) for key in ("slot_id", "path", "operation", "sha256", "size_bytes", "importance")}
        file["result_key"] = source.result_key
        await LoopEventJournal().append(session, source.loop_id, CanonicalEventDraft(
            kind="context.artifact.published", entity_type="artifact", entity_id=source.result_key,
            entity_revision=1, idempotency_key=f"file-version:{source.result_key}",
            payload={"context_id": source.context_id, "run_id": source.run_id, "file": file,
                "title": payload["path"], "summary": {"created": "新建文件", "modified": "修改文件", "deleted": "删除文件"}[payload["operation"]],
                "status": "observed"}))
