/**
 * 本文件对外提供 Focus 自有 Windows 授权的 prepare 与 release 命令。
 * 输入为真实 Workspace 与私有临时目录；输出为两处能力 SID 及授权事实，或带原生诊断的失败。
 * 具体工作流为先确认路径与隔离，再给 Workspace 持久授权、给 private temp 独立授权；释放时只撤销 private temp 授权。
 * 示例：node windows-sandbox-grant.mjs prepare C:\\work C:\\temp\\focus-sandbox-1。
 */
import { realpathSync } from "node:fs";
import { dirname } from "node:path";
import { privateTempSid, resolveBoundary } from "./windows-sandbox/boundary.mjs";
import { prepareGrant, releaseGrant } from "./windows-sandbox/grants.mjs";

const [action, workspaceInput, tempInput] = process.argv.slice(2);

try {
  if (!tempInput) throw new Error("private temp is required");
  if (action === "prepare") {
    if (!workspaceInput) throw new Error("Workspace is required");
    const boundary = resolveBoundary(workspaceInput, dirname(tempInput), tempInput);
    prepareGrant(boundary.temp, boundary.tempWriteSid);
    prepareGrant(boundary.workspace, boundary.writeSid);
    process.stdout.write(`${JSON.stringify({
      workspace: boundary.workspace, temp: boundary.temp,
      writeSid: boundary.writeSid, tempWriteSid: boundary.tempWriteSid,
    })}\n`);
  } else if (action === "release") {
    const temp = realpathSync.native(tempInput);
    releaseGrant(temp, privateTempSid(temp));
    process.stdout.write(`${JSON.stringify({ released: temp })}\n`);
  } else throw new Error(`Unknown grant action: ${action}`);
} catch (error) {
  process.stderr.write(`windows-sandbox-grant: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
