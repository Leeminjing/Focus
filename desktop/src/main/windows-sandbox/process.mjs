/**
 * 本文件对外提供 startManaged 的挂起创建、显式标准句柄继承、Job 纳管与退出等待。
 * 输入为受限令牌（完全访问时为 null）、目标 argv、工作区及启动回调；输出为可轮询、取消并等待整个普通后代进程树退出的对象。
 * 具体工作流为创建 kill-on-close Job，挂起创建目标，指派 Job 后恢复线程；finish 终止遗留后代并等待 ActiveProcesses 归零才关闭句柄。启动失败同时核实未纳管目标已退出；缺失证明不触发清理确认回调。
 * 示例：const running = startManaged(null, 'cmd.exe', ['/c', 'echo ok'], 'C:\\work'); const code = running.poll(); await running.finish()。
 */
import { win32 } from "./win32.mjs";

const n = win32();
const { api } = n;

function commandLine(command, args) {
  const quote = (value) => {
    const text = String(value);
    if (!/[\s"]/u.test(text)) return text;
    let result = '"', slashes = 0;
    for (const character of text) {
      if (character === "\\") { slashes += 1; continue; }
      if (character === '"') result += "\\".repeat(slashes * 2 + 1) + '"';
      else result += "\\".repeat(slashes) + character;
      slashes = 0;
    }
    return result + "\\".repeat(slashes * 2) + '"';
  };
  return [command, ...args].map(quote).join(" ");
}

function job() {
  const handle = api.createJobObjectW(null, null);
  if (n.isNull(handle)) throw n.failure("CreateJobObjectW");
  try {
    const limits = Buffer.alloc(144);
    limits.writeUInt32LE(0x2000, 16);
    n.check(api.setInformationJobObject(handle, 9, limits, limits.length), "SetInformationJobObject");
    return handle;
  } catch (error) { n.close(handle); throw error; }
}

function inheritedStdio() {
  const self = api.openProcess(0x40, 0, process.pid);
  if (n.isNull(self)) throw n.failure("OpenProcess", api.getLastError(), "duplicate standard handles");
  const handles = [];
  try {
    for (const selector of [-10, -11, -12]) {
      const original = api.getStdHandle(selector);
      if (n.isNull(original) || original === -1n) throw n.failure("GetStdHandle", api.getLastError(), String(selector));
      const output = n.slot();
      n.check(api.duplicateHandle(self, original, self, output, 0, 1, 2), "DuplicateHandle", String(selector));
      const duplicate = n.value(output);
      if (n.isNull(duplicate)) throw n.failure("DuplicateHandle", api.getLastError(), "null duplicate");
      handles.push(duplicate);
    }
    return handles;
  } catch (error) {
    for (const handle of handles) n.close(handle);
    throw error;
  } finally { n.close(self); }
}

function startup(handles) {
  const size = n.slot("size_t");
  api.initializeProcThreadAttributeList(null, 1, 0, size);
  const needed = Number(n.value(size, "size_t"));
  if (needed < 32 || needed > 1048576) throw n.failure("InitializeProcThreadAttributeList", api.getLastError(), String(needed));
  const attributes = Buffer.alloc(needed);
  n.check(api.initializeProcThreadAttributeList(attributes, 1, 0, size), "InitializeProcThreadAttributeList");
  try {
    const list = Buffer.alloc(handles.length * 8);
    handles.forEach((handle, index) => list.writeBigUInt64LE(n.address(handle), index * 8));
    n.check(api.updateProcThreadAttribute(attributes, 0, 0x20002, list, list.length, null, null), "UpdateProcThreadAttribute");
    const info = Buffer.alloc(112);
    info.writeUInt32LE(112, 0);
    info.writeUInt32LE(0x100, 60);
    handles.forEach((handle, index) => info.writeBigUInt64LE(n.address(handle), 80 + index * 8));
    info.writeBigUInt64LE(n.address(attributes), 104);
    return { info, attributes, list };
  } catch (error) {
    api.deleteProcThreadAttributeList(attributes);
    throw error;
  }
}

function targetProcess(token, command, args, cwd, handles, controllingJob, controls, proof) {
  const start = startup(handles);
  const output = n.slot(n.processInfo);
  let processHandle = null, threadHandle = null;
  try {
    const line = commandLine(command, args);
    const parameters = [null, line, null, null, 1, 0x8080004, null, cwd, start.info, output];
    n.check(token === null ? api.createProcessW(...parameters) : api.createProcessAsUserW(token, ...parameters),
      token === null ? "CreateProcessW" : "CreateProcessAsUserW", line);
    proof.created = true;
    const info = n.value(output, n.processInfo);
    processHandle = info.hProcess;
    threadHandle = info.hThread;
    controls.afterCreate?.(info.dwProcessId);
    if (n.isNull(processHandle) || n.isNull(threadHandle)) throw new Error("CreateProcessAsUserW returned missing handles");
    n.check((controls.assignProcess ?? api.assignProcessToJobObject)(controllingJob, processHandle, threadHandle),
      "AssignProcessToJobObject", `PID ${info.dwProcessId}`);
    controls.beforeResume?.(info.dwProcessId);
    if ((controls.resumeThread ?? api.resumeThread)(threadHandle) === 0xffffffff) {
      throw n.failure("ResumeThread", api.getLastError(), `PID ${info.dwProcessId}`);
    }
    n.close(threadHandle);
    threadHandle = null;
    const result = { pid: info.dwProcessId, handle: processHandle };
    processHandle = null;
    return result;
  } catch (error) {
    if (proof.created) {
      if (n.isNull(processHandle)) throw new Error("Created process cleanup handle unavailable");
      n.check(api.terminateProcess(processHandle, 1), "TerminateProcess");
      if (!n.isNull(threadHandle)) api.resumeThread(threadHandle);
      if (api.waitForSingleObject(processHandle, 0xffffffff) !== 0) throw n.failure("WaitForSingleObject");
      proof.exited = true;
    }
    throw error;
  } finally {
    if (!n.isNull(threadHandle)) n.close(threadHandle);
    if (!n.isNull(processHandle)) n.close(processHandle);
    api.deleteProcThreadAttributeList(start.attributes);
  }
}

function activeProcesses(handle) {
  const accounting = Buffer.alloc(48);
  n.check(api.queryInformationJobObject(handle, 1, accounting, accounting.length, null), "QueryInformationJobObject");
  return accounting.readUInt32LE(40);
}

export function startManaged(token, command, args, cwd, controls = {}) {
  let controllingJob = null;
  const proof = { created: false, exited: false };
  const handles = (controls.stdio ?? inheritedStdio)();
  try {
    controllingJob = (controls.job ?? job)();
    const process = targetProcess(token, command, args, cwd, handles, controllingJob, controls, proof);
    let closed = false;
    let terminating = false;
    return {
      pid: process.pid,
      poll() {
        const state = api.waitForSingleObject(process.handle, 0);
        if (state === 258) return null;
        if (state === 0xffffffff) throw n.failure("WaitForSingleObject");
        const output = n.slot("uint32");
        n.check(api.getExitCodeProcess(process.handle, output), "GetExitCodeProcess");
        return n.value(output, "uint32");
      },
      terminate() {
        if (closed || terminating) return;
        n.check(api.terminateJobObject(controllingJob, 1), "TerminateJobObject");
        terminating = true;
      },
      async finish() {
        if (closed) return [];
        if (!terminating && activeProcesses(controllingJob)) this.terminate();
        while (activeProcesses(controllingJob)) await new Promise(resolve => setTimeout(resolve, 20));
        closed = true;
        const warnings = [];
        try { n.close(controllingJob); }
        catch (error) {
          warnings.push(String(error));
          if (!api.terminateJobObject(controllingJob, 1)) warnings.push(String(n.failure("TerminateJobObject")));
        }
        try { n.close(process.handle); }
        catch (error) { warnings.push(String(error)); }
        return warnings;
      },
    };
  } catch (error) {
    if (!n.isNull(controllingJob)) {
      n.check(api.terminateJobObject(controllingJob, 1), "TerminateJobObject");
      while (activeProcesses(controllingJob)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
      n.close(controllingJob);
    }
    if (!proof.created || proof.exited) controls.afterCleanup?.();
    throw error;
  } finally {
    for (const handle of handles) n.close(handle);
  }
}
