"""本文件对外提供模型可见文件沙箱说明的内容测试。

输入为真实工作区、当前模式和能力列表；输出为模式、边界、最小放宽和重复副作用提示。
具体工作流为生成系统说明并核对关键事实，确保说明文本不被误当作授权来源。
示例：运行 python -m pytest backend/tests/test_sandbox_model_guidance.py。
"""

from focus.security.model_guidance import shell_policy_guidance
from focus.security.policy import AccessMode


def test_guidance_includes_current_mode_workspace_and_single_call_rules(tmp_path):
    text = shell_policy_guidance(tmp_path, AccessMode.READ_ONLY, ("read", "host_command"))
    assert "read-only" in text
    assert str(tmp_path) in text
    assert "host_command" in text
    assert "requested_mode" in text
    assert "最小放宽" in text
    assert "重复" in text
    assert "部分约束" in text
