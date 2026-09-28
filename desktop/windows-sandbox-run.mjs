/**
 * 本文件对外提供 Focus 自有 Windows 受限 Shell 的独立状态协议和目标启动入口。
 * 输入为状态文件、真实 Workspace、private temp、模式、能力 SID 与目标 argv；输出为准备/启动/退出或控制故障事实及目标标准流。
 * 具体工作流为先校验边界及能力，构造 Low 限制令牌并准备受控 Job/句柄，挂起创建并纳管目标，在状态确认后恢复执行；后续控制故障独立报告。
 * 示例：node windows-sandbox-run.mjs status.json C:\\work C:\\temp read-only "" "" -- cmd.exe /c echo ok。
 */
import { renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { resolveBoundary } from "./windows-sandbox/boundary.mjs";
import { createCallToken } from "./windows-sandbox/token.mjs";
import { startRestricted } from "./windows-sandbox/process.mjs";
import { win32 } from "./windows-sandbox/win32.mjs";

const [statusPath, workspaceInput, tempInput, mode, writeSid, tempSid, separator, command, ...args] = process.argv.slice(2);
const native = win32();
let phase = "preparing";
let targetResumed = false;
let targetExitCode = null;

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
  if (mode !== "read-only" && mode !== "workspace-write") throw new Error(`Invalid sandbox mode: ${mode}`);
  const boundary = mode === "workspace-write"
    ? resolveBoundary(workspaceInput, dirname(tempInput), tempInput)
    : resolveBoundary(workspaceInput, tempInput);
  if (mode === "workspace-write" && (writeSid !== boundary.writeSid || tempSid !== boundary.tempWriteSid)) {
    throw new Error("Sandbox capability SID does not match its directory");
  }
  if (mode === "read-only" && (writeSid || tempSid)) throw new Error("Read-only invocation carries write SIDs");
  const token = createCallToken(mode, mode === "workspace-write" ? writeSid : null,
    mode === "workspace-write" ? tempSid : null);
  try {
    if (mode === "workspace-write") {
      process.env.TEMP = boundary.temp;
      process.env.TMP = boundary.temp;
    }
    const target = startRestricted(token, command, args, boundary.workspace, {
      beforeResume: (pid) => report("started", { pid }),
      afterResume: () => { targetResumed = true; },
    });
    let cleanupWarnings = [];
    try {
      let exitCode = null;
      while (exitCode === null) {
        exitCode = target.poll();
        if (exitCode === null) await new Promise((resolve) => setTimeout(resolve, 20));
      }
      targetExitCode = exitCode;
      cleanupWarnings = target.close();
      report("exited", { pid: target.pid, exitCode, cleanupWarnings });
      process.exitCode = exitCode;
    } finally {
      if (phase !== "exited") cleanupWarnings = target.close();
    }
  } finally { native.close(token); }
}

try {
  report("preparing");
  await run();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  if (!targetResumed) {
    try { report("failed", { error: message }); }
    catch { process.stderr.write("windows-sandbox-run: status channel unavailable\n"); }
  } else {
    try { report("control_failed", { error: message, exitCode: targetExitCode }); }
    catch { process.stderr.write("windows-sandbox-run: post-start status unavailable\n"); }
  }
  process.stderr.write(`windows-sandbox-run: ${message}\n`);
  process.exitCode = targetResumed ? 126 : 127;
}
