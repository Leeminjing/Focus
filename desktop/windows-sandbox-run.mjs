/**
 * 本文件对外提供 Focus 自有 Windows 三档 Shell 的独立状态协议和目标启动入口。
 * 输入为状态文件、真实 Workspace、private temp、模式、能力 SID 与目标 argv；输出为准备/启动/退出或控制故障事实及目标标准流。
 * 具体工作流为校验边界及能力，受限模式构造 Low 令牌，完全访问沿用用户令牌；统一挂起纳入 Job 后执行，读取取消文件并等待整个 Job 无活动进程后报告 jobDrained。
 * 示例：node windows-sandbox-run.mjs status.json C:\\work C:\\temp read-only "" "" -- cmd.exe /c echo ok。
 */
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { resolveBoundary } from "./windows-sandbox/boundary.mjs";
import { createCallToken } from "./windows-sandbox/token.mjs";
import { startManaged } from "./windows-sandbox/process.mjs";
import { win32 } from "./windows-sandbox/win32.mjs";

const [statusPath, workspaceInput, tempInput, mode, writeSid, tempSid, separator, command, ...args] = process.argv.slice(2);
const native = win32();
let phase = "preparing";
let targetCreated = false;
let targetExitCode = null;
let targetDrained = false;

function report(next, fields = {}) {
  const pending = `${statusPath}.pending`;
  writeFileSync(pending, JSON.stringify({ phase: next, ...fields }), "utf8");
  renameSync(pending, statusPath);
  phase = next;
}

async function run() {
  if (!statusPath || !workspaceInput || !tempInput || separator !== "--" || !command) {
    throw new Error("Invalid sandbox invocation");
  }
  if (!["read-only", "workspace-write", "danger-full-access"].includes(mode)) throw new Error(`Invalid sandbox mode: ${mode}`);
  const boundary = mode === "workspace-write"
    ? resolveBoundary(workspaceInput, dirname(tempInput), tempInput)
    : resolveBoundary(workspaceInput, tempInput);
  if (mode === "workspace-write" && (writeSid !== boundary.writeSid || tempSid !== boundary.tempWriteSid)) {
    throw new Error("Sandbox capability SID does not match its directory");
  }
  if (mode !== "workspace-write" && (writeSid || tempSid)) throw new Error("Invocation carries unexpected write SIDs");
  const cancelled = () => existsSync(`${statusPath}.cancel`) ? readFileSync(`${statusPath}.cancel`, "utf8") || "cancelled" : null;
  if (cancelled()) { report("exited", { exitCode: 0, termination: cancelled(), jobDrained: true }); return; }
  const token = mode === "danger-full-access" ? null : createCallToken(mode, mode === "workspace-write" ? writeSid : null,
    mode === "workspace-write" ? tempSid : null);
  try {
    if (mode === "workspace-write") {
      process.env.TEMP = boundary.temp;
      process.env.TMP = boundary.temp;
    }
    const target = startManaged(token, command, args, boundary.workspace, {
      afterCreate: () => { targetCreated = true; },
      afterCleanup: () => { targetDrained = true; },
      beforeResume: (pid) => report("started", { pid }),
    });
    let cleanupWarnings = [];
    try {
      let exitCode = null;
      let termination = null;
      while (exitCode === null) {
        termination ||= cancelled();
        if (termination) target.terminate();
        exitCode = target.poll();
        if (exitCode === null) await new Promise((resolve) => setTimeout(resolve, 20));
      }
      targetExitCode = exitCode;
      cleanupWarnings = await target.finish();
      targetDrained = true;
      report("exited", { pid: target.pid, exitCode, cleanupWarnings, termination, jobDrained: true });
      process.exitCode = exitCode;
    } finally {
      if (phase !== "exited") { cleanupWarnings = await target.finish(); targetDrained = true; }
    }
  } finally { native.close(token); }
}

try {
  report("preparing");
  await run();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  if (!targetCreated) {
    try { report("failed", { error: message }); }
    catch { process.stderr.write("windows-sandbox-run: status channel unavailable\n"); }
  } else {
    try { report("control_failed", { error: message, exitCode: targetExitCode, jobDrained: targetDrained }); }
    catch { process.stderr.write("windows-sandbox-run: post-start status unavailable\n"); }
  }
  process.stderr.write(`windows-sandbox-run: ${message}\n`);
  process.exitCode = targetCreated ? 126 : 127;
}
