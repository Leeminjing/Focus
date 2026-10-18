"""Ordinary Run aggregation, pinned reads, and isolation from other work."""
import asyncio
from hashlib import sha256
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.task_changes import aggregate_versions, change_detail, line_counts, task_changes


def side(text=None, **extra):
    return {"available": True, "exists": text is not None, "text": text or "",
            "sha256": sha256(text.encode()).hexdigest() if text is not None else None, **extra}


def version(key, before, after, path="src/a.py", slot="slot"):
    return NS(result_key=key, payload={"format": "file_version_v1", "path": path,
              "slot_id": slot, "before": before, "after": after})


def test_net_changes_use_initial_baseline_and_final_snapshot():
    rows = [version("1", side("pre-existing dirty content\n"), side("intermediate\n")),
            version("2", side("intermediate\n"), side("final\n")),
            version("3", side(), side("new\n"), "new.py"),
            version("4", side("gone\n"), side(), "gone.py"),
            version("5", side(), side("temporary"), "temporary"),
            version("6", side("temporary"), side(), "temporary"),
            version("7", side("original"), side("edited"), "reverted"),
            version("8", side("edited"), side("original"), "reverted")]
    changes = aggregate_versions(rows)
    assert [item["path"] for item in changes] == ["src/a.py", "new.py", "gone.py"]
    assert (changes[0]["first_key"], changes[0]["last_key"]) == ("1", "2")
    assert changes[0]["before"]["text"] == "pre-existing dirty content\n"
    assert [(item["operation"], item["added"], item["removed"]) for item in changes] == [
        ("modified", 1, 1), ("created", 1, 0), ("deleted", 0, 1)]


def test_unavailable_or_interleaved_versions_never_invent_counts():
    changes = aggregate_versions([
        version("1", {"available": False, "reason": "before_unobserved"}, side("new")),
        version("2", side("old"), side("first"), "external"),
        version("3", side("external edit"), side("last"), "external"),
        version("4", side("old"), {"available": False, "exists": True, "sha256": "binary", "reason": "binary"}, "binary"),
        version("5", side("old"), side("new", redacted=True), "redacted"),
    ])
    assert [item["reason"] for item in changes] == ["before_unobserved", "discontinuous_versions", "binary", None]
    assert all(item["added"] is None and item["removed"] is None for item in changes[:3])
    assert changes[3]["redacted"]


@pytest.mark.parametrize("before,after,added,removed", [
    ("", "", 0, 0), ("", "\n", 1, 0), ("a", "a\n", 1, 1),
    ("a\r\nb\r\n", "a\r\nc\r\n", 1, 1),
    ("a\nb\na\n", "b\na\nb\n", 1, 1),
])
def test_line_counts_preserve_empty_files_and_line_terminators(before, after, added, removed):
    assert line_counts(before, after) == {"added": added, "removed": removed}


def test_counts_match_independent_lcs_oracle_and_are_bounded():
    import random
    random.seed(42)
    for _ in range(120):
        left = [random.choice("abc") + "\n" for _ in range(random.randrange(20))]
        right = [random.choice("abc") + "\n" for _ in range(random.randrange(20))]
        previous = [0] * (len(right) + 1)
        for a in left:
            current = [0]
            for index, b in enumerate(right):
                current.append(previous[index] + 1 if a == b else max(previous[index + 1], current[-1]))
            previous = current
        assert line_counts("".join(left), "".join(right)) == {"added": len(right) - previous[-1], "removed": len(left) - previous[-1]}
    assert line_counts("a" * 2097153, "") is None
    assert line_counts("a\n" * 100001, "") is None


def test_endpoints_pin_run_and_validate_ownership():
    first = version("first", side("old\n"), side("middle\n"))
    last = version("last", side("middle\n"), side("new\n"))
    for row in (first, last):
        row.run_id, row.context_id, row.kind = "run", "task", "artifact"
    run = NS(run_id="run", task_id="task", agent_id="main:task", kind="main", loop_id=None, status="success", workspace_anchor={"slot_id": "slot"})
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, model, key):
            if model is DesktopThread: return NS() if key == "task" else None
            if model is DesktopRun: return run if key == "run" else None
            if model is DesktopDomainResult: return {"first": first, "last": last}.get(key)
        async def scalar(self, statement): return run
        async def scalars(self, statement): return NS(all=lambda: [first, last])
    request = NS(app=NS(state=NS(desktop_service=NS(session_factory=Session))))
    async def check():
        overview = await task_changes("task", request)
        assert overview["run_id"] == "run"
        assert overview["files"][0]["added"] == 1
        assert "before" not in overview["files"][0]
        detail = await change_detail("task", "run", "first", "last", request)
        assert detail["before"]["text"] == "old\n" and detail["after"]["text"] == "new\n"
        for args in [("foreign", "run", "first", "last"), ("task", "foreign", "first", "last"), ("task", "run", "missing", "last")]:
            with pytest.raises(HTTPException) as error: await change_detail(*args, request)
            assert error.value.status_code == 404
        last.payload["path"] = "different"
        with pytest.raises(HTTPException) as error: await change_detail("task", "run", "first", "last", request)
        assert error.value.status_code == 409
        run.loop_id = "loop"
        assert await task_changes("task", request) == {"run_id": None, "files": []}
    asyncio.run(check())


def test_ordinary_tool_versions_flow_through_real_database(isolated_postgres_database, tmp_path):
    import uuid
    from langchain_core.messages import ToolMessage
    from backend.tests.test_workspace_patrol import _setup
    from backend.app.desktop.execution_attempts import ToolExecutionLedger
    from backend.app.desktop.workspace_coordination.models import WorkspaceSlot

    async def check():
        engine, sessions, workspace_id, _ = await _setup(tmp_path)
        task_id, slot_id, old_run, current_run = [uuid.uuid4().hex for _ in range(4)]
        try:
            async with sessions.begin() as session:
                session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=task_id, title="普通会话"))
                session.add(WorkspaceSlot(slot_id=slot_id, workspace_id=workspace_id, kind="authoritative", root_path=str(tmp_path), current_fingerprint="a" * 64))
            ledger = ToolExecutionLedger(sessions)
            path = tmp_path / "code.py"
            path.write_text("workspace already dirty\n", encoding="utf-8")
            for run_id, contents in [(old_run, ["old round\n"]), (current_run, ["intermediate\n", "final\n"])]:
                async with sessions.begin() as session:
                    session.add(DesktopRun(run_id=run_id, task_id=task_id, agent_id=f"main:{task_id}", kind="main", status="running", workspace_anchor={"slot_id": slot_id}))
                for index, content in enumerate(contents):
                    identity = sha256(f"{run_id}:{index}".encode()).hexdigest()
                    call = {"id": identity, "name": "write_file", "args": {"path": str(path), "content": content}}
                    assert await ledger.claim(identity, run_id, call) is None
                    capture = await ledger.prepare_file_capture(run_id)
                    capture.before_write(path)
                    path.write_text(content, encoding="utf-8", newline="")
                    capture.after_write(path)
                    await ledger.complete(identity, ToolMessage(content="written", tool_call_id=identity), file_capture=capture)
                async with sessions.begin() as session:
                    row = await session.get(DesktopRun, run_id)
                    row.status = "success"
            request = NS(app=NS(state=NS(desktop_service=NS(session_factory=sessions))))
            overview = await task_changes(task_id, request)
            assert overview["run_id"] == current_run and len(overview["files"]) == 1
            file = overview["files"][0]
            assert (file["added"], file["removed"]) == (1, 1)
            path.write_text("disk changed after run\n", encoding="utf-8")
            pinned = await change_detail(task_id, current_run, file["first_key"], file["last_key"], request)
            assert pinned["before"]["text"] == "old round\n"
            assert pinned["after"]["text"] == "final\n"
            with pytest.raises(HTTPException):
                await change_detail(task_id, old_run, file["first_key"], file["last_key"], request)
        finally:
            await engine.dispose()
    asyncio.run(check())
