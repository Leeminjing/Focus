/**
 * 本文件对外提供逐调用 Low、WRITE_RESTRICTED Windows 主令牌。
 * 输入为实际模式及本次 Workspace/private temp 能力 SID；输出为调用方负责关闭的受限令牌句柄。
 * 具体工作流为读取当前宿主令牌及登录 SID，选择本次限制 SID，建立限制令牌，补足新对象默认 DACL 并降低完整性。
 * 示例：const token = createCallToken('read-only', null, null); try { ... } finally { close(token) }。
 */
import { win32 } from "./win32.mjs";

const n = win32();
const { api, koffi } = n;

function sid(text) {
  const slot = n.slot();
  n.check(api.convertStringSidToSidW(text, slot), "ConvertStringSidToSidW", text);
  const pointer = n.value(slot);
  if (n.isNull(pointer)) throw n.failure("ConvertStringSidToSidW", api.getLastError(), text);
  return pointer;
}

function currentToken() {
  const processHandle = api.openProcess(0x400, 0, process.pid);
  if (n.isNull(processHandle)) throw n.failure("OpenProcess", api.getLastError(), `PID ${process.pid}`);
  try {
    const result = n.slot();
    n.check(api.openProcessToken(processHandle, 0x8b, result), "OpenProcessToken");
    const token = n.value(result);
    if (n.isNull(token)) throw n.failure("OpenProcessToken", api.getLastError(), "null token");
    return token;
  } finally { n.close(processHandle); }
}

function tokenData(token, kind) {
  const size = n.slot("uint32");
  api.getTokenInformation(token, kind, null, 0, size);
  const length = n.value(size, "uint32");
  if (length < 8 || length > 1048576) throw n.failure("GetTokenInformation", api.getLastError(), `kind ${kind} length ${length}`);
  const data = Buffer.alloc(length);
  n.check(api.getTokenInformation(token, kind, data, length, size), "GetTokenInformation", `kind ${kind}`);
  return data;
}

function logonSid(token) {
  const groups = tokenData(token, 2);
  const count = groups.readUInt32LE(0);
  if (count > (groups.length - 8) / 16) throw new Error("Invalid TokenGroups record");
  for (let index = 0; index < count; index += 1) {
    const offset = 8 + 16 * index;
    if ((groups.readUInt32LE(offset + 8) & 0xc0000000) >>> 0 !== 0xc0000000) continue;
    const pointer = koffi.decode(groups, offset, n.pointer);
    if (n.isNull(pointer)) continue;
    const length = api.getLengthSid(pointer);
    if (!length || length > 68) throw n.failure("GetLengthSid", api.getLastError(), "logon SID");
    const copy = n.bytes(length);
    n.check(api.copySid(length, copy, pointer), "CopySid", "logon SID");
    return copy;
  }
  throw new Error("TokenGroups contains no logon SID");
}

function restrictingRecord(pointers) {
  const result = Buffer.alloc(pointers.length * 16);
  pointers.forEach((pointer, index) => result.writeBigUInt64LE(n.address(pointer), index * 16));
  return result;
}

function defaultDacl(token, world) {
  const existing = tokenData(token, 6);
  const original = koffi.decode(existing, 0, n.pointer);
  if (n.isNull(original)) throw new Error("Restricted token has no default DACL");
  const entry = Buffer.alloc(48);
  entry.writeUInt32LE(0x1f01ff, 0);
  entry.writeUInt32LE(1, 4);
  entry.writeBigUInt64LE(n.address(world), 40);
  const mergedSlot = n.slot();
  const code = api.setEntriesInAclW(1, entry, original, mergedSlot);
  if (code !== 0) throw n.failure("SetEntriesInAclW", code, "token default DACL");
  const merged = n.value(mergedSlot);
  if (n.isNull(merged)) throw n.failure("SetEntriesInAclW", api.getLastError(), "null token default DACL");
  try {
    const info = Buffer.alloc(8);
    info.writeBigUInt64LE(n.address(merged));
    n.check(api.setTokenInformation(token, 6, info, info.length), "SetTokenInformation", "default DACL");
  } finally { n.free(merged); }
}

function lowIntegrity(token, low) {
  const length = api.getLengthSid(low);
  if (!length) throw n.failure("GetLengthSid", api.getLastError(), "Low integrity SID");
  const info = Buffer.alloc(16 + length);
  info.writeBigUInt64LE(n.address(low), 0);
  info.writeUInt32LE(0x20, 8);
  n.check(api.setTokenInformation(token, 25, info, info.length), "SetTokenInformation", "Low integrity");
}

export function createCallToken(mode, writeSid, tempSid) {
  if (mode !== "read-only" && mode !== "workspace-write") throw new Error(`Invalid sandbox mode: ${mode}`);
  if (mode === "workspace-write" && (!writeSid || !tempSid || writeSid === tempSid)) {
    throw new Error("Workspace write requires distinct Workspace and private temp SIDs");
  }
  if (mode === "read-only" && (writeSid || tempSid)) throw new Error("Read-only cannot carry write SIDs");
  const owned = [];
  let source = null, restricted = null;
  try {
    source = currentToken();
    const logon = logonSid(source);
    const world = sid("S-1-1-0"), low = sid("S-1-16-4096");
    owned.push(world, low);
    const capabilities = mode === "workspace-write" ? [sid(writeSid), sid(tempSid)] : [];
    owned.push(...capabilities);
    const list = restrictingRecord([logon, world, ...capabilities]);
    const output = n.slot();
    n.check(api.createRestrictedToken(source, 13, 0, null, 0, null,
      list.length / 16, list, output), "CreateRestrictedToken");
    restricted = n.value(output);
    if (n.isNull(restricted)) throw n.failure("CreateRestrictedToken", api.getLastError(), "null token");
    defaultDacl(restricted, world);
    lowIntegrity(restricted, low);
    const result = restricted;
    restricted = null;
    return result;
  } finally {
    if (restricted !== null) n.close(restricted);
    if (source !== null) n.close(source);
    for (const pointer of owned) n.free(pointer);
  }
}
