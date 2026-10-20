/* 桌面全局偏好固定存入 userData，仅暴露白名单键；同步原子写入后才确认保存。 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const KEYS = new Set([
  "focus-interface-language-v1", "focus-zoom-level", "focus-shell-layout",
  "focus-panel-width", "focus-map-view-v1", "focus-interaction-entry",
]);
class DesktopPreferences {
  constructor(directory) { this.file = path.join(directory, "desktop-preferences.json"); }
  read() {
    try {
      if (fs.statSync(this.file).size > 64 * 1024) throw new Error("桌面偏好文件过大");
      const value = JSON.parse(fs.readFileSync(this.file, "utf8"));
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("桌面偏好文件无效");
      return value;
    } catch (error) { if (error.code === "ENOENT") return {}; throw error; }
  }
  checkKey(key) { if (!KEYS.has(key)) throw new Error("桌面偏好键无效"); }
  getItem(key) { this.checkKey(key); return this.read()[key] ?? null; }
  setItem(key, value) {
    this.checkKey(key);
    if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 8192) throw new Error("桌面偏好值无效或过长");
    const values = this.read();
    if (values[key] === value) return;
    values[key] = value;
    const serialized = JSON.stringify(values);
    if (Buffer.byteLength(serialized, "utf8") > 64 * 1024) throw new Error("桌面偏好文件过大");
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const fd = fs.openSync(`${this.file}.tmp`, "w", 0o600);
    try { fs.writeFileSync(fd, serialized, "utf8"); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    fs.renameSync(`${this.file}.tmp`, this.file);
  }
}
function registerDesktopPreferences(ipcMain, { directory, authorize, onChange }) {
  for (const operation of ["getItem", "setItem"]) {
    ipcMain.on(`focus:preferences-${operation}`, (event, key, value) => {
      try {
        authorize(event);
        event.returnValue = { value: new DesktopPreferences(directory())[operation](key, value) ?? null };
        if (operation === "setItem") onChange?.({ key, value });
      } catch (error) { event.returnValue = { error: error.message }; }
    });
  }
}
module.exports = { DesktopPreferences, registerDesktopPreferences };
