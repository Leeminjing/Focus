"""本文件对外提供跨平台 ShellRecoveryEvidence 与 Windows 内核启动身份读取端口。

输入为逐 attempt 的持久执行身份；输出为匹配的终态回执、同机重启证据或核验失败。
具体工作流为 claim 预登记关联信息，runner 保存回执，恢复核验终态或内核启动 GUID；
回执缺失时仅请求遗留 runner 取消，不接受客户端路径或清理结论。
示例：proof = evidence.verify(execution, current_boot=windows_boot())。
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import uuid


def windows_boot() -> dict | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            machine = winreg.QueryValueEx(key, "MachineGuid")[0]
        # SYSTEM_BOOT_ENVIRONMENT_INFORMATION (90), layout from winsiderss/phnt/ntexapi.h.
        # A kernel boot GUID avoids wall-clock adjustments, PID reuse and WMI availability.
        class BootEnvironment(ctypes.Structure):
            _fields_ = [("identifier", ctypes.c_ubyte * 16), ("firmware", ctypes.c_uint32), ("flags", ctypes.c_uint64)]

        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_long
        data, size = BootEnvironment(), ctypes.c_uint32()
        if query(90, ctypes.byref(data), ctypes.sizeof(data), ctypes.byref(size)) < 0 or size.value != ctypes.sizeof(data):
            return None
        boot_id = uuid.UUID(bytes_le=bytes(data.identifier))
        return {"machine": machine, "boot_id": str(boot_id)} if boot_id.int else None
    except (AttributeError, OSError, ValueError):
        return None


class ShellRecoveryEvidence:
    def __init__(self, root: Path | None = None, *, boot_provider=windows_boot):
        self.root = root or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Focus/shell-receipts"
        self._boot = boot_provider

    def prepare(self, run_id: str, attempt_id: str, call_id: str) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        return {"run_id": run_id, "attempt_id": attempt_id, "call_id": call_id,
                "receipt_id": uuid.uuid4().hex, "boot": self._boot()}

    def path(self, evidence: dict) -> Path:
        receipt_id = evidence["receipt_id"]
        if not isinstance(receipt_id, str) or uuid.UUID(receipt_id).hex != receipt_id:
            raise ValueError("Shell 回执 identity 无效")
        return self.root / f"{receipt_id}.json"

    def verify(self, evidence: dict, *, current_boot: dict | None, reboot_baseline: dict | None = None) -> dict:
        previous = evidence.get("boot") or reboot_baseline
        if previous and current_boot and previous.get("machine") == current_boot.get("machine"):
            if previous.get("boot_id") and current_boot.get("boot_id") and previous["boot_id"] != current_boot["boot_id"]:
                return {"kind": "windows_reboot", "previous": previous, "current": current_boot}
        path = self.path(evidence)
        try:
            fact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fact = {}
        if isinstance(fact, dict) and fact.get("execution") == evidence:
            if fact.get("phase") == "failed" or (
                fact.get("phase") in {"exited", "control_failed"}
                and (fact.get("jobDrained") is True or fact.get("processGroupDrained") is True)
            ):
                return {"kind": "runner_receipt", "receipt": fact}
        # A request to stop is never exit evidence. A later verification must read a terminal receipt.
        path.with_name(path.name + ".cancel").write_text("cancelled", encoding="utf-8")
        detail = "若回执损坏或 runner 已崩溃，请重启 Windows 后核验" if os.name == "nt" else "若控制进程已崩溃，需人工核对遗留进程；当前平台不支持通过重启自动核验"
        raise ValueError(f"尚无匹配的进程树退出证据；已请求遗留 runner 停止，请稍后再核验。{detail}")
