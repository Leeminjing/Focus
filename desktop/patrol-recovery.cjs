/* 本文件提供桌面 Patrol 的本地恢复仓库。
 * 输入为版本化草稿/提交快照和无路径附件字节；输出为 userData 中原子替换的快照与有界临时材料。
 * 写入先落附件再提交引用，提交后清理无引用文件；启动清理中断留下的孤儿。受引用材料不按时间过期。
 * 示例：recovery.put(id, bytes); recovery.save({version:1, workspaces:[], source_ids:[id]})。
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024;
class PatrolRecovery {
  constructor(directory, byteLimit) {
    this.directory = path.join(directory, "patrol-recovery");
    this.byteLimit = byteLimit;
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    this.snapshotPath = path.join(this.directory, "state.json");
  }
  sourcePath(id) {
    if (typeof id !== "string" || !id || id.length > 160) throw new Error("恢复材料身份无效");
    return path.join(this.directory, `${createHash("sha256").update(id).digest("hex")}.bin`);
  }
  atomicWrite(file, bytes) {
    const fd = fs.openSync(`${file}.tmp`, "w", 0o600);
    try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(`${file}.tmp`, file);
  }
  read() {
    let state = null;
    try {
      if (fs.statSync(this.snapshotPath).size > MAX_SNAPSHOT_BYTES) throw new Error("恢复快照超过大小上限");
      state = JSON.parse(fs.readFileSync(this.snapshotPath, "utf8"));
      this.validate(state);
    } catch (error) { if (error.code !== "ENOENT") throw new Error(`无法读取 Patrol 恢复数据：${error.message}`); }
    const sources = [];
    let total = 0;
    for (const id of state?.source_ids || []) {
      const file = this.sourcePath(id);
      total += fs.statSync(file).size;
      if (total > this.byteLimit) throw new Error("恢复材料总量超限");
      sources.push([id, new Uint8Array(fs.readFileSync(file))]);
    }
    this.cleanup(state?.source_ids || []);
    return { state, sources };
  }
  validate(state) {
    if (state?.version !== 1 || !Array.isArray(state.workspaces) || !Array.isArray(state.source_ids) || state.source_ids.length > 4096) throw new Error("恢复快照格式无效");
    for (const id of state.source_ids) this.sourcePath(id);
  }
  put(id, bytes) {
    const file = this.sourcePath(id);
    if (!(bytes instanceof Uint8Array) || !bytes.byteLength) throw new Error("恢复材料字节无效");
    let total = bytes.byteLength;
    for (const name of fs.readdirSync(this.directory)) if (name.endsWith(".bin") && path.join(this.directory, name) !== file) total += fs.statSync(path.join(this.directory, name)).size;
    if (total > this.byteLimit) throw new Error("本地恢复材料总量超限，请发送或移除已有材料");
    this.atomicWrite(file, bytes);
    return true;
  }
  save(state) {
    this.validate(state);
    const bytes = Buffer.from(JSON.stringify(state));
    if (bytes.length > MAX_SNAPSHOT_BYTES) throw new Error("本地恢复记录已满，请处理未确认的发送");
    for (const id of state.source_ids) if (!fs.existsSync(this.sourcePath(id))) throw new Error("恢复材料缺失，无法保存草稿");
    this.atomicWrite(this.snapshotPath, bytes);
    // 快照提交后，清理失败不能使调用方回滚到旧引用。
    try { this.cleanup(state.source_ids); } catch (error) { console.error("清理 Patrol 临时材料失败:", error.message); }
    return true;
  }
  cleanup(ids) {
    const used = new Set(ids.map(id => path.basename(this.sourcePath(id))));
    for (const name of fs.readdirSync(this.directory)) {
      if ((/^[a-f0-9]{64}\.bin(?:\.tmp)?$/.test(name) && !used.has(name)) || name === "state.json.tmp") fs.unlinkSync(path.join(this.directory, name));
    }
  }
}
module.exports = { PatrolRecovery };
