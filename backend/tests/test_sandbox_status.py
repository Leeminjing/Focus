"""本文件对外提供 Windows 文件沙箱状态及限制文案的只读测试。

输入为工作区准备记录和固定后端路径；输出为部分约束、未应用沙箱及边界列表。
具体工作流为替换只读登记来源，调用状态构造器并核对用户可见字段。
示例：运行 python -m pytest backend/tests/test_sandbox_status.py。
"""

import focus.sandbox.status as status_module


def test_status_lists_prepared_roots_and_partial_limitations(monkeypatch):
    class Registry:
        def list(self):
            return ("C:/workspace-a", "C:/workspace-b")

    monkeypatch.setattr(status_module, "PreparedWorkspaceRegistry", Registry)
    state = status_module.sandbox_status()
    assert state["prepared_workspaces"] == ["C:/workspace-a", "C:/workspace-b"]
    assert state["unrestricted_label"] == "未应用文件沙箱"
    assert state["enforcement"] in ("partial", "unavailable")
    combined = " ".join(state["limitations"])
    for boundary in (
        "读取保密", "网络隔离", "硬链接", "CIM/WMI", "Low", "竞态",
        "完整虚拟机", "独立 Windows 用户", "曾尝试准备",
    ):
        assert boundary in combined
