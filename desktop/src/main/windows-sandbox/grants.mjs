/**
 * 本文件对外提供 Workspace 常驻写授权与 private temp 可撤销写授权。
 * 输入为已校验的真实目录和各自能力 SID；输出为 Win32 DACL/Low 标签调整结果或明确的原生错误。
 * 具体工作流为锁定目录安全描述符，合并能力写入及删除授权、普通删除拒绝和 Low 继承标签；释放时仅撤销 private temp 授权。
 * 示例：prepareGrant('C:\\work', 'S-1-4-1-2-1'); releaseGrant('C:\\temp\\private', 'S-1-4-3-4-2')。
 */
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { win32 } from "./win32.mjs";

const n = win32();
const { api, koffi } = n;
const grantMask = 0x110156;
const deleteChild = 0x40;
const fileObject = 1;
const daclInfo = 4;
const labelInfo = 16;

function sidPointer(text) {
  const output = n.slot();
  n.check(api.convertStringSidToSidW(text, output), "ConvertStringSidToSidW", text);
  const pointer = n.value(output);
  if (n.isNull(pointer)) throw n.failure("ConvertStringSidToSidW", api.getLastError(), text);
  return pointer;
}

function explicitEntry(sid, mode, mask, inheritance) {
  const entry = Buffer.alloc(48);
  entry.writeUInt32LE(mask, 0);
  entry.writeUInt32LE(mode, 4);
  entry.writeUInt32LE(inheritance, 8);
  entry.writeBigUInt64LE(n.address(sid), 40);
  return entry;
}

function aclEntryExists(acl, kind, inheritance, mask, sid) {
  if (n.isNull(acl)) return false;
  const size = koffi.decode(acl, 2, "uint16");
  const count = koffi.decode(acl, 4, "uint16");
  if (size < 8 || size > 1048576 || count > 8192) return false;
  const sidSize = api.getLengthSid(sid);
  let offset = 8;
  for (let index = 0; index < count; index += 1) {
    if (offset + 8 > size) return false;
    const length = koffi.decode(acl, offset + 2, "uint16");
    if (length < 8 || offset + length > size) return false;
    if (length >= sidSize + 8 && koffi.decode(acl, offset, "uint8") === kind &&
        koffi.decode(acl, offset + 1, "uint8") === inheritance &&
        koffi.decode(acl, offset + 4, "uint32") === mask) {
      let equal = true;
      for (let byte = 0; byte < sidSize; byte += 1) {
        if (koffi.decode(acl, offset + 8 + byte, "uint8") !== koffi.decode(sid, byte, "uint8")) {
          equal = false;
          break;
        }
      }
      if (equal) return true;
    }
    offset += length;
  }
  return false;
}

function currentSecurity(path) {
  const daclSlot = n.slot(), saclSlot = n.slot(), descriptorSlot = n.slot();
  const code = api.getNamedSecurityInfoW(path, fileObject, daclInfo | labelInfo,
    null, null, daclSlot, saclSlot, descriptorSlot);
  if (code !== 0) throw n.failure("GetNamedSecurityInfoW", code, path);
  return {
    dacl: n.value(daclSlot), sacl: n.value(saclSlot), descriptor: n.value(descriptorSlot),
  };
}

function lowLabel(sid) {
  const length = api.getLengthSid(sid);
  if (length === 0) throw n.failure("GetLengthSid");
  const size = 16 + length;
  const acl = api.localAlloc(64, size);
  if (n.isNull(acl)) throw n.failure("LocalAlloc", api.getLastError(), "mandatory label ACL");
  try {
    n.check(api.initializeAcl(acl, size, 2), "InitializeAcl");
    n.check(api.addMandatoryAce(acl, 2, 3, 1, sid), "AddMandatoryAce");
    return acl;
  } catch (error) {
    n.free(acl);
    throw error;
  }
}

function edit(path, capability, revoke) {
  const world = sidPointer("S-1-1-0"), low = sidPointer("S-1-16-4096");
  try {
    const security = currentSecurity(path);
    let merged = null, label = null;
    try {
      if (!revoke && aclEntryExists(security.dacl, 0, 3, grantMask, capability) &&
          aclEntryExists(security.dacl, 1, 2, deleteChild, world) &&
          aclEntryExists(security.sacl, 17, 3, 1, low)) return;
      const entries = revoke
        ? explicitEntry(capability, 4, 0, 3)
        : Buffer.concat([explicitEntry(world, 3, deleteChild, 2), explicitEntry(capability, 1, grantMask, 3)]);
      const mergedSlot = n.slot();
      const mergeCode = api.setEntriesInAclW(entries.length / 48, entries, security.dacl, mergedSlot);
      if (mergeCode !== 0) throw n.failure("SetEntriesInAclW", mergeCode, path);
      merged = n.value(mergedSlot);
      if (n.isNull(merged)) throw n.failure("SetEntriesInAclW", api.getLastError(), "null ACL");
      if (!revoke) label = lowLabel(low);
      const code = api.setNamedSecurityInfoW(path, fileObject,
        daclInfo | (revoke ? 0 : labelInfo), null, null, merged, label);
      if (code !== 0) throw n.failure("SetNamedSecurityInfoW", code, path);
    } finally {
      n.free(label);
      n.free(merged);
      n.free(security.descriptor);
    }
  } finally {
    n.free(low);
    n.free(world);
  }
}

function withDirectoryLock(path, action) {
  const digest = createHash("sha256").update(path.toLowerCase()).digest("hex").slice(0, 24);
  const lockRoot = join(tmpdir(), "focus-sandbox-locks");
  mkdirSync(lockRoot, { recursive: true });
  const handle = api.createFileW(join(lockRoot, `${digest}.lock`), 0xc0000000, 3, null, 4, 0, null);
  if (n.isNull(handle) || handle === -1n || handle === 18446744073709551615n) {
    throw n.failure("CreateFileW", api.getLastError(), path);
  }
  const overlapped = Buffer.alloc(32);
  try {
    n.check(api.lockFileEx(handle, 2, 0, 1, 0, overlapped), "LockFileEx", path);
    try { return action(); }
    finally { n.check(api.unlockFileEx(handle, 0, 1, 0, overlapped), "UnlockFileEx", path); }
  } finally {
    n.close(handle);
  }
}

export function prepareGrant(path, sid) {
  const capability = sidPointer(sid);
  try { return withDirectoryLock(path, () => edit(path, capability, false)); }
  finally { n.free(capability); }
}

export function releaseGrant(path, sid) {
  const capability = sidPointer(sid);
  try { return withDirectoryLock(path, () => edit(path, capability, true)); }
  finally { n.free(capability); }
}
