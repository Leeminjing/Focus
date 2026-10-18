"""验证默认代码工具的分页、忽略/输出边界、精确编辑和真实权限/版本观察。

输入为临时工作区和生产签发上下文；输出为可继续的行游标、实际字节 diff 与失败零写入。
示例：python -m pytest backend/tests/test_workspace_code_tools.py -q。
"""

import json
from dataclasses import replace
from hashlib import sha256
import os
import stat
import subprocess

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langchain_core.utils.function_calling import convert_to_openai_tool

from backend.tests.runtime_context_support import tool_runtime
from backend.app.desktop.domain_evidence.file_versions import FileVersionCapture
from focus.security.authority import AuthorityKind, AuthoritySurface
from focus.security.context import security_context_of
from focus.security.effects import effect_of, resolve_fs_effect
from focus.security.middleware import AccessPolicyMiddleware, _admit
from focus.security.policy import AccessMode
from focus.tools.builtins import workspace_search, workspace_text
from focus.tools.builtins.workspace_tools import edit_file, read_file_lines, search_files, search_text


def runtime(root, permissions=("read", "write"), **kwargs):
    extras = kwargs.pop("extras", {})
    rt = tool_runtime(agent_id="main:code-tools", task_id="code-tools", workspace=str(root),
                      permissions=permissions, **kwargs)
    # 文件观察端口由执行层在身份签发后注入，不能作为普通载荷伪造受治理键。
    security = security_context_of(rt.context)
    return replace(rt, context={**rt.context, **replace(security, extras={**security.extras, **extras}).to_runtime_context()})


def call(tool, root, **args):
    return json.loads(tool.invoke({**args, "runtime": runtime(root)}))


def test_read_pages_and_version_guard(tmp_path):
    target = tmp_path / "code.py"
    target.write_bytes("第一行\r\nsecond\nthird".encode())
    first = call(read_file_lines, tmp_path, path="code.py", max_lines=2)
    assert first["lines"] == [{"line": 1, "column": 1, "text": "第一行", "line_ending": "\r\n"},
                              {"line": 2, "column": 1, "text": "second", "line_ending": "\n"}]
    assert first["next_line"] == 3 and first["next_column"] == 1
    assert first["sha256"] == sha256(target.read_bytes()).hexdigest()
    last = call(read_file_lines, tmp_path, path="code.py", start_line=3,
                expected_sha256=first["sha256"])
    assert last["lines"][0]["text"] == "third"
    assert not last["truncated"] and last["next_line"] is None
    target.write_text("changed", encoding="utf-8")
    with pytest.raises(ToolException, match="版本不匹配"):
        read_file_lines.func(path="code.py", runtime=runtime(tmp_path), expected_sha256=first["sha256"])


def test_long_line_can_be_continued_without_losing_content(tmp_path):
    text = "长" * (workspace_text.MAX_OUTPUT_CHARS + 10) + "\nend\n"
    (tmp_path / "long.txt").write_bytes(text.encode())
    first = call(read_file_lines, tmp_path, path="long.txt")
    assert first["next_line"] == 1
    second = call(read_file_lines, tmp_path, path="long.txt", start_line=first["next_line"],
                  start_column=first["next_column"], expected_sha256=first["sha256"])
    assert first["lines"][0]["text"] + second["lines"][0]["text"] == text.splitlines()[0]
    assert second["lines"][-1]["text"] == "end" and not second["truncated"]


def test_physical_lines_round_trip_into_multiline_edit(tmp_path):
    target = tmp_path / "a"
    original = "\ufeffone\r\ntwo\x0c\u2028same\rthree\nlast".encode()
    target.write_bytes(original)
    page = call(read_file_lines, tmp_path, path="a")
    assert page["utf8_bom"] and page["total_lines"] == 4
    block = "".join(line["text"] + line["line_ending"] for line in page["lines"])
    assert block.encode() == original[3:]
    hit = call(search_text, tmp_path, path="a", query="same")["matches"][0]
    assert hit["line"] == 2 and hit["column"] == 6
    result = call(edit_file, tmp_path, path="a", expected_sha256=page["sha256"],
                  edits=[{"old_text": block, "new_text": block.replace("three", "new")}])
    assert target.read_bytes() == original.replace(b"three", b"new")
    assert "@@ -1,4 +1,4 @@" in result["diff"]


@pytest.mark.parametrize("raw", [b"", b"\n", b"a\n", b"a"])
def test_eof_page_is_explicit(tmp_path, raw):
    (tmp_path / "a").write_bytes(raw)
    page = call(read_file_lines, tmp_path, path="a", start_line=len(raw.decode().splitlines()) + 1)
    assert page["lines"] == [] and page["next_line"] is None and not page["truncated"]


@pytest.mark.parametrize("args", [{"start_line": 0}, {"start_column": 0}, {"max_lines": 501},
                                 {"start_line": 99}, {"start_column": 99}])
def test_invalid_read_range_is_recoverable(tmp_path, args):
    (tmp_path / "a").write_text("hello")
    result = read_file_lines.invoke({"type": "tool_call", "id": "bad", "name": "read_file_lines",
                                    "args": {"path": "a", "runtime": runtime(tmp_path), **args}})
    assert isinstance(result, ToolMessage) and result.status == "error"


@pytest.mark.parametrize("raw", [b"bad\0text", b"\xff", b"x" * (workspace_text.MAX_FILE_BYTES + 1)],
                         ids=["binary", "non_utf8", "too_large"])
def test_unsupported_text_rejected_without_modification(tmp_path, raw):
    target = tmp_path / "a"
    target.write_bytes(raw)
    with pytest.raises(ToolException):
        read_file_lines.func(path="a", runtime=runtime(tmp_path))
    with pytest.raises(ToolException):
        edit_file.func(path="a", edits=[{"old_text": "x", "new_text": "y"}], runtime=runtime(tmp_path))
    assert target.read_bytes() == raw


def test_batch_edits_preserve_bom_mixed_newlines_and_final_newline(tmp_path):
    target = tmp_path / "code.py"
    original = b"\xef\xbb\xbfif first:\r\n    pass\nif second:\r\n    pass"
    target.write_bytes(original)
    result = call(edit_file, tmp_path, path="code.py", expected_sha256=sha256(original).hexdigest(),
                  edits=[{"old_text": "if first:", "new_text": "if ready:"},
                         {"old_text": "if second:", "new_text": "if done:"}])
    expected = original.replace(b"if first:", b"if ready:").replace(b"if second:", b"if done:")
    assert target.read_bytes() == expected
    assert result["before_sha256"] == sha256(original).hexdigest()
    assert result["sha256"] == sha256(expected).hexdigest()
    assert "-if second:" in result["diff"] and "+if done:" in result["diff"]
    assert not result["diff_truncated"] and result["changed"]


@pytest.mark.parametrize("edits,expected,reason", [
    ([{"old_text": "missing", "new_text": "x"}], None, "旧内容不匹配"),
    ([{"old_text": "a", "new_text": "x"}], None, "多处匹配"),
    ([{"old_text": "aa", "new_text": "x"}], None, "多处匹配"),
    ([{"old_text": "abc", "new_text": "x"}, {"old_text": "bcd", "new_text": "y"}], None, "重叠"),
    ([{"old_text": "unique", "new_text": "x"}, {"old_text": "missing", "new_text": "y"}], None, "不匹配"),
    ([{"old_text": "unique", "new_text": "x"}], "0" * 64, "版本不匹配"),
    ([{"old_text": "", "new_text": "x"}], None, "不能为空"),
    ([], None, "1..50"),
])
def test_failed_edit_batch_never_writes(tmp_path, edits, expected, reason):
    target = tmp_path / "a"
    original = b"aaa abcd unique\r\n"
    target.write_bytes(original)
    capture = FileVersionCapture("slot", tmp_path)
    rt = runtime(tmp_path, extras={"file_version_capture": capture})
    with pytest.raises(ToolException, match=reason):
        edit_file.func(path="a", edits=edits, runtime=rt, expected_sha256=expected)
    assert target.read_bytes() == original and capture.changes == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]


def test_edits_match_original_not_output_of_prior_edits(tmp_path):
    (tmp_path / "a").write_text("one two")
    call(edit_file, tmp_path, path="a", edits=[{"old_text": "one", "new_text": "two"},
                                              {"old_text": "two", "new_text": "three"}])
    assert (tmp_path / "a").read_text() == "two three"


def test_edit_records_real_bytes_and_noop_records_nothing(tmp_path):
    target = tmp_path / "a"
    original = b"old\r\nmixed\n"
    target.write_bytes(original)
    capture = FileVersionCapture("slot", tmp_path)
    rt = runtime(tmp_path, extras={"file_version_capture": capture})
    edit_file.func(path="a", edits=[{"old_text": "old", "new_text": "new"}], runtime=rt)
    assert len(capture.changes) == 1
    change = capture.changes[0]
    assert change["operation"] == "modified"
    assert change["before"]["sha256"] == sha256(original).hexdigest()
    assert change["after"]["sha256"] == sha256(target.read_bytes()).hexdigest()
    assert change["after"]["text"] == "new\r\nmixed\n"
    result = json.loads(edit_file.func(path="a", edits=[{"old_text": "new", "new_text": "new"}], runtime=rt))
    assert not result["changed"] and not result["diff"] and len(capture.changes) == 1


def test_atomic_edit_failure_leaves_original_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "a"
    target.write_text("old")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(workspace_text.os, "replace", fail)
    with pytest.raises(ToolException, match="replace failed"):
        edit_file.func(path="a", edits=[{"old_text": "old", "new_text": "new"}], runtime=runtime(tmp_path))
    assert target.read_text() == "old" and list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation")
@pytest.mark.parametrize("protected", [True, False])
def test_edit_preserves_explicit_windows_dacl_before_staging_content(tmp_path, monkeypatch, protected):
    security = pytest.importorskip("win32security")
    api = pytest.importorskip("win32api")
    constants = pytest.importorskip("win32con")
    access = pytest.importorskip("ntsecuritycon")
    target = tmp_path / "a"
    target.write_text("old")
    token = security.OpenProcessToken(api.GetCurrentProcess(), constants.TOKEN_QUERY)
    sid = security.GetTokenInformation(token, security.TokenUser)[0]
    dacl = security.ACL()
    dacl.AddAccessAllowedAce(security.ACL_REVISION, access.FILE_ALL_ACCESS, sid)
    protection = (security.PROTECTED_DACL_SECURITY_INFORMATION if protected
                  else security.UNPROTECTED_DACL_SECURITY_INFORMATION)
    security.SetNamedSecurityInfo(str(target), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | protection,
        None, None, dacl, None)
    def descriptor(path):
        sd = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
        return security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            sd, security.SDDL_REVISION_1, security.DACL_SECURITY_INFORMATION)
    before = descriptor(target)
    replace_file = workspace_text.os.replace
    def checked_replace(source, destination):
        assert descriptor(source) == before
        replace_file(source, destination)
    monkeypatch.setattr(workspace_text.os, "replace", checked_replace)
    call(edit_file, tmp_path, path="a", edits=[{"old_text": "old", "new_text": "new"}])
    assert target.read_text() == "new" and descriptor(target) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode")
def test_edit_preserves_executable_mode(tmp_path):
    target = tmp_path / "a"
    target.write_text("old")
    target.chmod(0o751)
    call(edit_file, tmp_path, path="a", edits=[{"old_text": "old", "new_text": "new"}])
    assert stat.S_IMODE(target.stat().st_mode) == 0o751


def test_file_changed_during_capture_is_not_overwritten(tmp_path):
    target = tmp_path / "a"
    target.write_text("old")
    class Capture:
        def before_write(self, path):
            path.write_text("external writer")
        def after_write(self, *args):
            pytest.fail("failed edit must not record")
    with pytest.raises(ToolException, match="编辑期间变化"):
        edit_file.func(path="a", edits=[{"old_text": "old", "new_text": "new"}],
                       runtime=runtime(tmp_path, extras={"file_version_capture": Capture()}))
    assert target.read_text() == "external writer" and list(tmp_path.iterdir()) == [target]


def test_diff_is_bounded_and_marks_missing_final_newline(tmp_path):
    (tmp_path / "a").write_bytes(b"old")
    result = call(edit_file, tmp_path, path="a", edits=[{"old_text": "old", "new_text": "x" * 30000}])
    assert result["diff_truncated"] and len(result["diff"]) <= workspace_text.MAX_OUTPUT_CHARS
    assert "No newline at end of file" in result["diff"]


def test_nested_ignore_rules_negation_and_subdirectory_root(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n*.log\n!keep.log\n")
    (tmp_path / ".ignore").write_text("ignored.txt\n")
    (tmp_path / ".rgignore").write_text("rgignored.txt\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".gitignore").write_text("nested.py\n!keep.log\n")
    for name in ["main.py", "a.log", "keep.log", "ignored.txt", "rgignored.txt", "src/nested.py", "src/ok.py", "src/a.log", "src/keep.log"]:
        (tmp_path / name).write_text("needle\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("needle")
    result = call(search_files, tmp_path)
    assert {m["path"] for m in result["matches"]} == {"main.py", "keep.log", "src/ok.py", "src/keep.log"}
    assert not result["incomplete"]
    nested = call(search_text, tmp_path, path="src", query="needle", file_pattern="*.py")
    assert [m["path"] for m in nested["matches"]] == ["ok.py"]
    recursive_glob = call(search_files, tmp_path, pattern="**/*.py")
    assert {m["path"] for m in recursive_glob["matches"]} == {"main.py", "src/ok.py"}


def test_search_returns_literal_matches_line_column_context_and_hidden_option(tmp_path):
    (tmp_path / "a.py").write_bytes("before\r\nİ a.*b\nAFTER\n".encode())
    (tmp_path / ".hidden.py").write_text("a.*b")
    result = call(search_text, tmp_path, query="A.*B", case_sensitive=False, file_pattern="*.py", context_lines=1)
    match = result["matches"][0]
    assert match["path"] == "a.py" and match["line"] == 2 and match["column"] == 3
    assert [c["text"] for c in match["context"]] == ["before", "İ a.*b", "AFTER"]
    hidden = call(search_files, tmp_path, pattern="*.py", include_hidden=True)
    assert {m["path"] for m in hidden["matches"]} == {"a.py", ".hidden.py"}
    assert call(search_text, tmp_path, path="a.py", query="before")["matches"][0]["line"] == 1


def test_search_limits_and_skips_are_explicit(tmp_path, monkeypatch):
    for name in ["a.py", "b.py", "c.py"]:
        (tmp_path / name).write_text("needle")
    limited = call(search_text, tmp_path, query="needle", max_results=1)
    assert len(limited["matches"]) == 1 and limited["limit_reasons"] == ["result_limit"]
    (tmp_path / "binary").write_bytes(b"needle\0")
    (tmp_path / "invalid").write_bytes(b"\xffneedle")
    result = call(search_text, tmp_path, query="needle")
    assert result["skipped"] == {"binary": 1, "non_utf8": 1} and result["incomplete"]
    monkeypatch.setattr(workspace_search, "MAX_ENTRIES", 2)
    assert "entry_limit" in call(search_files, tmp_path)["limit_reasons"]


def test_search_byte_time_and_depth_limits(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("needle")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_text("needle")
    monkeypatch.setattr(workspace_search, "MAX_TOTAL_BYTES", 3)
    assert "byte_limit" in call(search_text, tmp_path, query="needle")["limit_reasons"]
    monkeypatch.setattr(workspace_search, "MAX_SECONDS", 0)
    assert "time_limit" in call(search_files, tmp_path)["limit_reasons"]
    monkeypatch.setattr(workspace_search, "MAX_SECONDS", 5)
    monkeypatch.setattr(workspace_search, "MAX_DEPTH", 0)
    assert "depth_limit" in call(search_files, tmp_path)["limit_reasons"]


@pytest.mark.parametrize("args", [{"query": ""}, {"query": "a\nb"}, {"query": "a", "max_results": 201},
                                 {"query": "a", "context_lines": 6}, {"query": "a", "file_pattern": ""}])
def test_invalid_search_is_recoverable(tmp_path, args):
    result = search_text.invoke({"type": "tool_call", "id": "bad-search", "name": "search_text",
                                 "args": {**args, "runtime": runtime(tmp_path)}})
    assert result.status == "error"


def test_search_does_not_follow_directory_links(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "secret.txt").write_text("needle")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation not available")
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                       check=True, capture_output=True)
    result = call(search_text, tmp_path, query="needle")
    assert result["matches"] == [] and result["skipped"]["linked_or_special"] == 1


@pytest.mark.parametrize("tool,args", [(read_file_lines, {"path": "a"}), (search_files, {}),
    (search_text, {"query": "a"}), (edit_file, {"path": "a", "edits": [{"old_text": "a", "new_text": "b"}]})])
def test_tools_enforce_capabilities_and_export_model_schema(tmp_path, tool, args):
    with pytest.raises(PermissionError, match="read"):
        tool.func(runtime=runtime(tmp_path, ()), **args)
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert "runtime" not in schema["properties"]
    effect = resolve_fs_effect(effect_of(tool), args, runtime(tmp_path).context)
    target = (tmp_path / args.get("path", ".")).resolve()
    if tool in (search_files, search_text):
        assert effect.read_trees == (target,)
    else:
        assert effect.reads == (target,)
        assert effect.writes == ((target,) if tool is edit_file else ())


def test_edit_permission_and_read_only_mode_are_enforced_before_io(tmp_path):
    args = {"path": "a", "edits": [{"old_text": "old", "new_text": "new"}]}
    target = tmp_path / "a"
    target.write_text("old")
    with pytest.raises(PermissionError, match="write"):
        edit_file.func(**args, runtime=runtime(tmp_path, ("read",)))
    request = ToolCallRequest(tool_call={"name": "edit_file", "id": "denied", "args": args},
                              tool=edit_file, state={}, runtime=runtime(tmp_path, access_mode=AccessMode.READ_ONLY))
    result = AccessPolicyMiddleware().wrap_tool_call(request, lambda _: pytest.fail("denied edit executed"))
    assert result.status == "error" and "FILE_POLICY_DENIED" in result.content
    assert target.read_text() == "old"


def test_recursive_search_admission_checks_sensitive_descendants(tmp_path, monkeypatch):
    from focus.security import middleware
    policy = security_context_of(runtime(tmp_path).context).policy()
    secret = tmp_path / "nested" / "credentials"
    surface = AuthoritySurface(AuthorityKind.CREDENTIALS, secret, readable=False)
    monkeypatch.setattr(middleware, "policy_from_context", lambda _: replace(policy, authority=(surface,)))
    request = ToolCallRequest(tool_call={"name": "search_text", "id": "search", "args": {"query": "secret"}},
                              tool=search_text, state={}, runtime=runtime(tmp_path))
    admission = _admit(request)
    assert admission.asked and secret in admission.reads and admission.args["path"] == str(tmp_path)
