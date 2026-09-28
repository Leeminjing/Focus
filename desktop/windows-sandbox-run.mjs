/**
 * 本文件对外提供固定 DSH AclSandbox 的受限进程启动适配器。
 * 输入为服务端分配的状态文件、真实工作区、临时根、文件模式、能力 SID 与目标 argv。
 * 输出为独立状态文件中的准备/启动/退出事实，以及继承的目标标准输出和错误。
 * 具体工作流为核对路径与 SID，初始化 DSH 限制令牌，挂起创建并纳入 Job 后运行目标，
 * 随后记录真实目标退出码；任何准备失败均写入状态文件并以 127 退出。
 * 示例：node windows-sandbox-run.mjs status.json C:\work C:\temp read-only "" "" -- cmd.exe /c echo ok。
 */
import { realpathSync, writeFileSync } from "node:fs";
import { AclSandbox, assertTempRootOutsideWorkspace, tempWriteSid, workspaceWriteSid } from "@deepseek-ai/dsh-sandbox-windows-acl";

const [statusPath, workspaceInput, tempInput, mode, writeSid, tempSid, separator, command, ...args] = process.argv.slice(2);

function report(phase, fields = {}) {
  writeFileSync(statusPath, JSON.stringify({ phase, ...fields }), "utf8");
}

async function main() {
  if (!statusPath || !workspaceInput || !tempInput || separator !== "--" || !command) {
    throw new Error("invalid sandbox invocation");
  }
  if (mode !== "read-only" && mode !== "workspace-write") {
    throw new Error(`invalid sandbox mode: ${mode}`);
  }
  const workspace = realpathSync.native(workspaceInput);
  const temp = realpathSync.native(tempInput);
  if (mode === "workspace-write") {
    assertTempRootOutsideWorkspace(workspace, temp);
    if (writeSid !== workspaceWriteSid(workspace) || tempSid !== tempWriteSid(temp)) {
      throw new Error("sandbox capability SID does not match its directory");
    }
  } else if (writeSid || tempSid) {
    throw new Error("read-only invocation must not carry write capability SIDs");
  }
  const sandbox = new AclSandbox({
    writableDirs: mode === "workspace-write" ? [workspace] : [],
    tempDir: mode === "workspace-write" ? temp : null,
    mode,
    manageDacls: false,
    ...(mode === "workspace-write" ? { writeSid, tempWriteSid: tempSid } : {}),
  });
  let initialized = false;
  try {
    await sandbox.init();
    initialized = true;
    if (mode === "workspace-write") {
      process.env.TEMP = temp;
      process.env.TMP = temp;
    }
    const child = sandbox.spawn({ command, args, cwd: workspace, stdio: "inherit" });
    report("started", { pid: child.pid });
    const result = await child.wait();
    report("exited", { pid: child.pid, exitCode: result.exitCode });
    process.exitCode = result.exitCode;
  } finally {
    if (initialized) {
      try {
        sandbox.dispose();
      } catch (error) {
        process.stderr.write(`windows-sandbox-run cleanup: ${String(error)}\n`);
      }
    }
  }
}

try {
  report("preparing");
  await main();
} catch (error) {
  try {
    report("failed", { error: String(error) });
  } catch {
    process.stderr.write("windows-sandbox-run: status channel unavailable\n");
  }
  process.stderr.write(`windows-sandbox-run: ${String(error)}\n`);
  process.exitCode = 127;
}
