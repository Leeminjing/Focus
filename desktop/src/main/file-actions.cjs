/* Native file operations accept an API-owned target, never a renderer path.
 * Saved historical text is exported exactly as displayed, including redaction.
 */
"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");

function createFileActions({ shell, dialog, BrowserWindow, apiBase, session, fetch: request = fetch }) {
  const temporaryDirectories = new Set();
  async function api(endpoint, target) {
    const response = await request(`${apiBase()}/desktop/api/files/${endpoint}`, {
      method: "POST", headers: { "X-Focus-Session": session(), "Content-Type": "application/json" }, body: JSON.stringify(target),
    });
    if (!response.ok) {
      let detail; try { detail = (await response.json()).detail; } catch {}
      throw new Error(typeof detail === "string" ? detail : `文件操作失败 (${response.status})`);
    }
    return response;
  }
  async function run(event, { operation, target, name }) {
    if (!["open", "reveal", "save", "html"].includes(operation)) throw new Error("未知文件操作");
    const parent = BrowserWindow.fromWebContents(event.sender);
    // An explicit current-file request keeps its history identity for path resolution.
    if (operation === "reveal" || (operation === "open" && (target.current === true || !target.loop_id))) {
      const location = await (await api("location", { ...target, current: true })).json();
      if (operation === "reveal") shell.showItemInFolder(location.path);
      else { const error = await shell.openPath(location.path); if (error) throw new Error(error); }
      return true;
    }
    const safeName = path.basename(String(name || "file").replace(/\\/g, "/")).replace(/[<>:"|?*\x00-\x1f]/g, "_");
    let destination;
    if (operation === "save") {
      const result = await dialog.showSaveDialog(parent, { defaultPath: safeName });
      if (result.canceled) return false;
      destination = result.filePath;
    }
    const response = await api("content", target);
    const bytes = Buffer.from(await response.arrayBuffer());
    if (operation === "html") {
      const preview = new BrowserWindow({ parent, width: 1000, height: 760, title: safeName,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, javascript: false } });
      preview.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      preview.webContents.on("will-navigate", event => event.preventDefault());
      const csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'\">";
      await preview.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(csp + bytes.toString("utf8"))}`);
      return true;
    }
    if (!destination) {
      const directory = await fs.mkdtemp(path.join(os.tmpdir(), "focus-file-"));
      temporaryDirectories.add(directory);
      destination = path.join(directory, safeName);
    }
    await fs.writeFile(destination, bytes);
    if (operation === "open") { const error = await shell.openPath(destination); if (error) throw new Error(error); }
    return true;
  }
  async function cleanup() {
    await Promise.allSettled([...temporaryDirectories].map(directory => fs.rm(directory, { recursive: true, force: true })));
  }
  return { run, cleanup };
}
module.exports = { createFileActions };
