/* Native artifact exits resolve persisted identities via the authenticated backend.
 * Real paths and hashes are checked again after save dialogs; historical snapshots
 * remain previews and are never silently replaced with different disk bytes. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
async function digest(file) {
  const hash = crypto.createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

function createArtifactFiles({ resolve, shell, dialog }) {
  async function checked(reference) {
    if (!reference || !["loopId", "factId"].every(key => typeof reference[key] === "string" && reference[key].length > 0 && reference[key].length <= 128)
      || !Number.isSafeInteger(reference.revision) || reference.revision < 1) throw new Error("无效的文件版本身份");
    const file = await resolve({ loopId: reference.loopId, factId: reference.factId, revision: reference.revision });
    const root = await fs.promises.realpath(file.root_path);
    const target = await fs.promises.realpath(file.path);
    const relative = path.relative(root, target);
    if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)
      || !(await fs.promises.stat(target)).isFile()) throw new Error("文件已移动、删除或超出工作区，请刷新文件列表");
    if (await digest(target) !== file.sha256) throw new Error("磁盘文件已有更新，请刷新后选择最新版本；历史版本仍可查看快照");
    return { path: target, sha256: file.sha256 };
  }
  return {
    async open(reference) {
      const error = await shell.openPath((await checked(reference)).path);
      if (error) throw new Error(error);
      return { ok: true };
    },
    async reveal(reference) { shell.showItemInFolder((await checked(reference)).path); return { ok: true }; },
    async saveAs(reference, parent) {
      const source = await checked(reference);
      const result = await dialog.showSaveDialog(parent, { title: "另存产物", defaultPath: path.basename(source.path), properties: ["showOverwriteConfirmation"] });
      if (result.canceled || !result.filePath) return { canceled: true };
      const current = await checked(reference);
      const destination = path.resolve(result.filePath);
      const existing = await fs.promises.realpath(destination).catch(error => { if (error.code === "ENOENT") return null; throw error; });
      if (existing === current.path) throw new Error("请选择与原文件不同的另存位置");
      // Verify the copied bytes before replacing the user's chosen destination.
      const temporary = path.join(path.dirname(destination), `.focus-${crypto.randomUUID()}.tmp`);
      try {
        await fs.promises.copyFile(current.path, temporary, fs.constants.COPYFILE_EXCL);
        if (await digest(temporary) !== current.sha256) throw new Error("复制期间文件发生变化，请刷新后重试另存");
        await fs.promises.rename(temporary, destination);
      } finally { await fs.promises.unlink(temporary).catch(error => { if (error.code !== "ENOENT") throw error; }); }
      return { ok: true };
    },
  };
}
module.exports = { createArtifactFiles };
