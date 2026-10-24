/**
 * 本文件对外提供真实 Windows 目录边界、重叠校验与 Workspace/private temp 能力 SID。
 * 输入为已存在的工作区、公共临时根和执行会话私有临时目录；输出为规范目录及各自独立的 SID。
 * 具体工作流为解析最终路径并确认目录类型，拒绝任意授权区域重叠，再按目录身份生成稳定或一次性能力。
 * 示例：const boundary = resolveBoundary('C:\\work', 'C:\\temp', 'C:\\temp\\focus-sandbox-1')。
 */
import { createHash } from "node:crypto";
import { realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, sep } from "node:path";

function directory(input) {
  const result = realpathSync.native(input);
  if (!statSync(result).isDirectory()) throw new Error(`Not a directory: ${result}`);
  return result;
}

function contains(root, candidate) {
  const difference = relative(root, candidate);
  return difference === "" || (!isAbsolute(difference) && difference !== ".." && !difference.startsWith(`..${sep}`));
}

function sid(domain, path) {
  const digest = createHash("sha256").update(domain).update("\0").update(path.toLowerCase()).digest();
  return `S-1-4-${digest.readUInt32LE(0) || 1}-${digest.readUInt32LE(4) || 1}-${domain === "temp" ? 2 : 1}`;
}

export function resolveBoundary(workspaceInput, tempRootInput, privateTempInput) {
  const workspace = directory(workspaceInput);
  const tempRoot = directory(tempRootInput);
  if (contains(workspace, tempRoot)) {
    throw new Error("Workspace contains temp root");
  }
  const temp = privateTempInput === undefined ? null : directory(privateTempInput);
  if (temp !== null && (!contains(tempRoot, temp) || contains(workspace, temp) || contains(temp, workspace))) {
    throw new Error("Private temp boundary is outside its root or overlaps Workspace");
  }
  return Object.freeze({
    workspace, tempRoot, temp,
    writeSid: sid("workspace", workspace),
    tempWriteSid: temp === null ? null : sid("temp", temp),
  });
}

export function privateTempSid(tempInput) {
  return sid("temp", directory(tempInput));
}
