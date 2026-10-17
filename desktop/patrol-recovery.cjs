/* 本文件提供桌面 Patrol 的本地恢复仓库。
 * 输入为版本化草稿/提交快照和无路径附件字节；输出为 userData 中原子替换的快照与有界临时材料。
 * 写入先落附件再提交引用，提交后清理无引用文件；启动清理中断留下的孤儿。受引用材料不按时间过期。
 * 写入在单一异步队列中执行，通过 FileHandle.sync 确认耐久；示例：await recovery.put(id, bytes); await recovery.save({version:1, workspaces:[], source_ids:[id]})。
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
    this.writes = Promise.resolve();
    this.uncommittedSources = new Set();
  }
  sourcePath(id) {
    if (typeof id !== "string" || !id || id.length > 160) throw new Error("恢复材料身份无效");
    return path.join(this.directory, `${createHash("sha256").update(id).digest("hex")}.bin`);
  }
  enqueue(operation) {
    const result = this.writes.then(operation);
    this.writes = result.catch(() => {});
    return result;
  }
  async atomicWrite(file, bytes) {
    const fd = await fs.promises.open(`${file}.tmp`, "w", 0o600);
    try { await fd.writeFile(bytes); await fd.sync(); } finally { await fd.close(); }
    await fs.promises.rename(`${file}.tmp`, file);
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
    // 同一宿主内的 renderer 重载可能与异步写入重叠；只在首次启动读取时清理孤儿。
    if (!this.loaded) { this.cleanup(state?.source_ids || []); this.loaded = true; }
    return { state, sources };
  }
  validate(state) {
    if (state?.version !== 1 || !Array.isArray(state.workspaces) || !Array.isArray(state.source_ids) || state.source_ids.length > 4096) throw new Error("恢复快照格式无效");
    for (const id of state.source_ids) this.sourcePath(id);
  }
  put(id, bytes) {
    const file = this.sourcePath(id);
    if (!(bytes instanceof Uint8Array) || !bytes.byteLength) throw new Error("恢复材料字节无效");
    const copy = Buffer.from(bytes);
    return this.enqueue(async () => {
      let total = copy.byteLength;
      for (const name of await fs.promises.readdir(this.directory)) if (name.endsWith(".bin") && path.join(this.directory, name) !== file) total += (await fs.promises.stat(path.join(this.directory, name))).size;
      if (total > this.byteLimit) throw new Error("本地恢复材料总量超限，请发送或移除已有材料");
      await this.atomicWrite(file, copy);
      // 较早捕获的草稿快照可能排在 put 后；首次提交引用前不能清理这些字节。
      this.uncommittedSources.add(id);
      return true;
    });
  }
  save(state) {
    this.validate(state);
    const bytes = Buffer.from(JSON.stringify(state));
    if (bytes.length > MAX_SNAPSHOT_BYTES) throw new Error("本地恢复记录已满，请处理未确认的发送");
    const ids = [...state.source_ids];
    return this.enqueue(async () => {
      for (const id of ids) {
        try { await fs.promises.access(this.sourcePath(id)); }
        catch { throw new Error("恢复材料缺失，无法保存草稿"); }
      }
      await this.atomicWrite(this.snapshotPath, bytes);
      ids.forEach(id => this.uncommittedSources.delete(id));
      // 快照提交后，清理失败不能使调用方回滚到旧引用。
      try { await this.cleanupAsync([...ids, ...this.uncommittedSources]); }
      catch (error) { console.error("清理 Patrol 临时材料失败:", error.message); }
      return true;
    });
  }
  async cleanupAsync(ids) {
    const used = new Set(ids.map(id => path.basename(this.sourcePath(id))));
    for (const name of await fs.promises.readdir(this.directory)) {
      if ((/^[a-f0-9]{64}\.bin(?:\.tmp)?$/.test(name) && !used.has(name)) || name === "state.json.tmp") await fs.promises.unlink(path.join(this.directory, name));
    }
  }
  cleanup(ids) {
    const used = new Set(ids.map(id => path.basename(this.sourcePath(id))));
    for (const name of fs.readdirSync(this.directory)) {
      if ((/^[a-f0-9]{64}\.bin(?:\.tmp)?$/.test(name) && !used.has(name)) || name === "state.json.tmp") fs.unlinkSync(path.join(this.directory, name));
    }
  }
}
module.exports = { PatrolRecovery };
