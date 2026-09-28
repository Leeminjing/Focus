/**
 * 本文件对外提供固定 DSH ACL 授权的 prepare 与 release 命令。
 * 输入为服务端确认的真实工作区和独立私有临时目录；释放时只需私有临时目录。
 * 输出为两者的规范路径及能力 SID，
 * 或包含具体 Win32 诊断的失败状态。具体工作流为 prepare 按固定 DSH 原语写入常驻
 * 工作区授权和可撤销临时授权，release 撤销临时授权而保留工作区授权。
 * 示例：node windows-sandbox-grant.mjs prepare C:\work C:\temp\private。
 */
import { realpathSync } from "node:fs";
import { isAbsolute, relative, sep } from "node:path";
import {
  AclWriteGrant,
  assertTempRootOutsideWorkspace,
  tempWriteSid,
  workspaceWriteSid,
} from "@deepseek-ai/dsh-sandbox-windows-acl";

function paths(workspaceInput, tempInput) {
  const workspace = realpathSync.native(workspaceInput);
  const temp = realpathSync.native(tempInput);
  assertTempRootOutsideWorkspace(workspace, temp);
  const relation = relative(temp, workspace);
  if (relation === "" || (!isAbsolute(relation) && relation !== ".." && !relation.startsWith(`..${sep}`))) {
    throw new Error("private temp and workspace overlap");
  }
  return { workspace, temp, writeSid: workspaceWriteSid(workspace), tempWriteSid: tempWriteSid(temp) };
}

function prepare(boundary) {
  const workspaceGrant = AclWriteGrant.create(boundary.writeSid);
  workspaceGrant.add(boundary.workspace, true);
  const tempGrant = AclWriteGrant.create(boundary.tempWriteSid);
  try {
    tempGrant.add(boundary.temp);
  } catch (error) {
    tempGrant.dispose();
    throw error;
  }
  process.stdout.write(`${JSON.stringify(boundary)}\n`);
}

function release(boundary) {
  const tempGrant = AclWriteGrant.create(boundary.tempWriteSid);
  tempGrant.add(boundary.temp);
  tempGrant.dispose();
  process.stdout.write(`${JSON.stringify({ released: boundary.temp })}\n`);
}

const [action, workspaceInput, tempInput] = process.argv.slice(2);
try {
  if (!tempInput) throw new Error("private temp is required");
  if (action === "prepare") {
    if (!workspaceInput) throw new Error("workspace is required");
    prepare(paths(workspaceInput, tempInput));
  } else if (action === "release") {
    const temp = realpathSync.native(tempInput);
    release({ temp, tempWriteSid: tempWriteSid(temp) });
  }
  else throw new Error(`unknown action: ${action}`);
} catch (error) {
  process.stderr.write(`windows-sandbox-grant: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
