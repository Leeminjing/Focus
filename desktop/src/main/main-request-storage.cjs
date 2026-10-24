/* 普通任务未确认请求的本机仓库：按 task 隔离，固定在 Electron userData 下。
 * 输入为受限恢复键和序列化快照；输出为同步存储接口和仅授权窗口可用的 IPC。
 * 投递前先 fsync 临时文件再原子替换，读写失败必须上抛，不能退回临时 origin 并生成新身份。
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const MAX_BYTES = 8 * 1024 * 1024;

class MainRequestStorage {
  constructor(userData) {
    this.directory = path.join(userData, "main-request-recovery");
  }
  file(key) {
    if (typeof key !== "string" || !key.startsWith("focus-main-request:")
      || key.length <= "focus-main-request:".length || key.length > 512) throw new Error("普通任务恢复键无效");
    return path.join(this.directory, `${createHash("sha256").update(key).digest("hex")}.json`);
  }
  validate(value) {
    if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > MAX_BYTES) throw new Error("普通任务恢复记录超过大小上限");
    const saved = JSON.parse(value);
    if (saved?.version !== 1 || typeof saved.request_id !== "string" || !saved.request_id
      || typeof saved.body !== "string") throw new Error("普通任务恢复数据无法读取，请保留原请求后重试");
    return value;
  }
  getItem(key) {
    const file = this.file(key);
    try {
      if (fs.statSync(file).size > MAX_BYTES) throw new Error("普通任务恢复记录超过大小上限");
      return this.validate(fs.readFileSync(file, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw error;
    }
  }
  setItem(key, value) {
    const file = this.file(key);
    this.validate(value);
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    const fd = fs.openSync(`${file}.tmp`, "w", 0o600);
    try {
      fs.writeFileSync(fd, value, "utf8");
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    fs.renameSync(`${file}.tmp`, file);
  }
  removeItem(key) {
    const file = this.file(key);
    try { fs.unlinkSync(file); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
}

function registerMainRequestStorage(ipcMain, { directory, authorize }) {
  for (const operation of ["getItem", "setItem", "removeItem"]) {
    ipcMain.on(`focus:main-request-${operation}`, (event, key, value) => {
      try {
        authorize(event);
        const storage = new MainRequestStorage(directory());
        event.returnValue = { value: storage[operation](key, value) ?? null };
      } catch (error) { event.returnValue = { error: error.message }; }
    });
  }
}
module.exports = { MainRequestStorage, registerMainRequestStorage };
