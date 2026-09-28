/**
 * 本文件对外提供 Windows 沙箱所需的 Win32 Koffi 绑定、原生缓冲区与受控句柄工具。
 * 输入为明确的 Win32 API 名称、指针和调用结果；输出为检查后的原生调用或带 API 与错误码的异常。
 * 具体工作流为加载系统 DLL，核对 64 位 ABI，集中声明函数签名，并由调用方显式关闭其拥有的句柄。
 * 示例：const { api, slot, value, check } = win32(); check(api.openProcessToken(handle, 8, slot()), "OpenProcessToken")。
 */
import koffi from "koffi";

const pointer = koffi.pointer("void");
const pointerPointer = koffi.pointer(pointer);
const dwordPointer = koffi.pointer("uint32");
const kernel32 = koffi.load("kernel32.dll");
const advapi32 = koffi.load("advapi32.dll");
const bind = (library, name, result, args) => library.func("__stdcall", name, result, args);

const startupInfo = koffi.struct("FOCUS_STARTUPINFOW", {
  cb: "uint32", lpReserved: "str16", lpDesktop: "str16", lpTitle: "str16",
  dwX: "uint32", dwY: "uint32", dwXSize: "uint32", dwYSize: "uint32",
  dwXCountChars: "uint32", dwYCountChars: "uint32", dwFillAttribute: "uint32",
  dwFlags: "uint32", wShowWindow: "uint16", cbReserved2: "uint16",
  lpReserved2: pointer, hStdInput: pointer, hStdOutput: pointer, hStdError: pointer,
});
const processInfo = koffi.struct("FOCUS_PROCESS_INFORMATION", {
  hProcess: pointer, hThread: pointer, dwProcessId: "uint32", dwThreadId: "uint32",
});
if (process.arch !== "x64" || startupInfo.size !== 104 || processInfo.size !== 24) {
  throw new Error("Unsupported Windows sandbox ABI: x64 STARTUPINFOW/PROCESS_INFORMATION required");
}

const api = Object.freeze({
  getLastError: bind(kernel32, "GetLastError", "uint32", []),
  closeHandle: bind(kernel32, "CloseHandle", "int", [pointer]),
  localFree: bind(kernel32, "LocalFree", pointer, [pointer]),
  localAlloc: bind(kernel32, "LocalAlloc", pointer, ["uint32", "size_t"]),
  openProcess: bind(kernel32, "OpenProcess", pointer, ["uint32", "int", "uint32"]),
  getProcessId: bind(kernel32, "GetProcessId", "uint32", [pointer]),
  openThread: bind(kernel32, "OpenThread", pointer, ["uint32", "int", "uint32"]),
  getThreadId: bind(kernel32, "GetThreadId", "uint32", [pointer]),
  openProcessToken: bind(advapi32, "OpenProcessToken", "int", [pointer, "uint32", pointerPointer]),
  convertStringSidToSidW: bind(advapi32, "ConvertStringSidToSidW", "int", ["str16", pointerPointer]),
  createWellKnownSid: bind(advapi32, "CreateWellKnownSid", "int", ["int", pointer, pointer, dwordPointer]),
  getLengthSid: bind(advapi32, "GetLengthSid", "uint32", [pointer]),
  copySid: bind(advapi32, "CopySid", "int", ["uint32", pointer, pointer]),
  getTokenInformation: bind(advapi32, "GetTokenInformation", "int", [pointer, "int", pointer, "uint32", dwordPointer]),
  setTokenInformation: bind(advapi32, "SetTokenInformation", "int", [pointer, "int", pointer, "uint32"]),
  createRestrictedToken: bind(advapi32, "CreateRestrictedToken", "int", [pointer, "uint32", "uint32", pointer, "uint32", pointer, "uint32", pointer, pointerPointer]),
  getNamedSecurityInfoW: bind(advapi32, "GetNamedSecurityInfoW", "uint32", ["str16", "int", "uint32", pointerPointer, pointerPointer, pointerPointer, pointerPointer, pointerPointer]),
  setNamedSecurityInfoW: bind(advapi32, "SetNamedSecurityInfoW", "uint32", ["str16", "int", "uint32", pointer, pointer, pointer, pointer]),
  setEntriesInAclW: bind(advapi32, "SetEntriesInAclW", "uint32", ["uint32", pointer, pointer, pointerPointer]),
  initializeAcl: bind(advapi32, "InitializeAcl", "int", [pointer, "uint32", "uint32"]),
  addMandatoryAce: bind(advapi32, "AddMandatoryAce", "int", [pointer, "uint32", "uint32", "uint32", pointer]),
  createJobObjectW: bind(kernel32, "CreateJobObjectW", pointer, [pointer, "str16"]),
  setInformationJobObject: bind(kernel32, "SetInformationJobObject", "int", [pointer, "int", pointer, "uint32"]),
  assignProcessToJobObject: bind(kernel32, "AssignProcessToJobObject", "int", [pointer, pointer]),
  terminateJobObject: bind(kernel32, "TerminateJobObject", "int", [pointer, "uint32"]),
  resumeThread: bind(kernel32, "ResumeThread", "uint32", [pointer]),
  terminateProcess: bind(kernel32, "TerminateProcess", "int", [pointer, "uint32"]),
  waitForSingleObject: bind(kernel32, "WaitForSingleObject", "uint32", [pointer, "uint32"]),
  getExitCodeProcess: bind(kernel32, "GetExitCodeProcess", "int", [pointer, dwordPointer]),
  getStdHandle: bind(kernel32, "GetStdHandle", pointer, ["int"]),
  getHandleInformation: bind(kernel32, "GetHandleInformation", "int", [pointer, dwordPointer]),
  setHandleInformation: bind(kernel32, "SetHandleInformation", "int", [pointer, "uint32", "uint32"]),
  duplicateHandle: bind(kernel32, "DuplicateHandle", "int", [pointer, pointer, pointer, pointerPointer, "uint32", "int", "uint32"]),
  createFileW: bind(kernel32, "CreateFileW", pointer, ["str16", "uint32", "uint32", pointer, "uint32", "uint32", pointer]),
  lockFileEx: bind(kernel32, "LockFileEx", "int", [pointer, "uint32", "uint32", "uint32", "uint32", pointer]),
  unlockFileEx: bind(kernel32, "UnlockFileEx", "int", [pointer, "uint32", "uint32", "uint32", pointer]),
  createProcessAsUserW: bind(advapi32, "CreateProcessAsUserW", "int", [pointer, "str16", "str16", pointer, pointer, "int", "uint32", pointer, "str16", koffi.pointer(startupInfo), koffi.pointer(processInfo)]),
  initializeProcThreadAttributeList: bind(kernel32, "InitializeProcThreadAttributeList", "int", [pointer, "uint32", "uint32", koffi.pointer("size_t")]),
  updateProcThreadAttribute: bind(kernel32, "UpdateProcThreadAttribute", "int", [pointer, "uint32", "size_t", pointer, "size_t", pointer, pointer]),
  deleteProcThreadAttributeList: bind(kernel32, "DeleteProcThreadAttributeList", "void", [pointer]),
});

function slot(type = pointer) { return koffi.alloc(type, 1); }
function value(address, type = pointer) { return koffi.decode(address, type); }
function address(pointerValue) { return koffi.address(pointerValue); }
function bytes(length) { return koffi.alloc("uint8", length); }
function isNull(handle) { return handle === null || handle === undefined || handle === 0n; }
function failure(name, code = api.getLastError(), detail = "") {
  const error = new Error(`${name} failed (Win32 ${code})${detail ? `: ${detail}` : ""}`);
  error.win32Code = code;
  return error;
}
function check(result, name, detail = "") {
  if (!result) throw failure(name, api.getLastError(), detail);
  return result;
}
function close(handle, name = "CloseHandle") {
  if (!isNull(handle)) check(api.closeHandle(handle), name);
}
function free(handle, name = "LocalFree") {
  if (!isNull(handle) && !isNull(api.localFree(handle))) throw failure(name);
}

export function win32() {
  return { api, koffi, pointer, startupInfo, processInfo, slot, value, address, bytes, isNull, failure, check, close, free };
}
