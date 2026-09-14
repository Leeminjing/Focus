/**
 * 本文件对外提供 MaterialContentLoader，用认证请求把任务材料转换为可显示的 Blob URL。
 *
 * 输入为 apiBase、桌面 session、taskId/materialId 和待绑定的 img 节点；输出为按材料缓存的
 * object URL 或已填充 src 的图片节点。具体工作流为使用 X-Focus-Session 请求任务作用域内容
 * API，成功后缓存 URL；材料删除、任务切换或页面卸载时统一 revoke，session 不进入 URL/DOM。
 *
 * 示例：await loader.bindAll(document); loader.releaseTask(taskId)。
 */
(function (global) {
  "use strict";

  class MaterialContentLoader {
    constructor({ apiBase, session, fetchImpl }) {
      this.apiBase = String(apiBase || "").replace(/\/$/, "");
      this.session = session;
      this.fetchImpl = fetchImpl || (global.fetch
        ? global.fetch.bind(global)
        : async () => { throw new Error("当前环境不支持 fetch"); });
      this.entries = new Map();
    }

    async load(taskId, materialId) {
      const key = this._key(taskId, materialId);
      const current = this.entries.get(key);
      if (current) return current.promise;
      const promise = this._fetch(taskId, materialId).catch(error => {
        this.entries.delete(key);
        throw error;
      });
      this.entries.set(key, { taskId, materialId, promise, url: null });
      const url = await promise;
      const entry = this.entries.get(key);
      if (!entry) {
        URL.revokeObjectURL(url);
        throw new Error("材料内容已释放");
      }
      entry.url = url;
      return url;
    }

    async bindAll(root) {
      const scope = root || document;
      if (!scope || typeof scope.querySelectorAll !== "function") return;
      const nodes = [...scope.querySelectorAll("img[data-material-content-id]")];
      await Promise.all(nodes.map(async node => {
        try {
          const url = await this.load(node.dataset.materialTaskId, node.dataset.materialContentId);
          if (node.isConnected) {
            node.src = url;
            node.dataset.imageUrl = url;
          }
        } catch {
          if (node.isConnected) node.classList.add("is-unavailable");
        }
      }));
    }

    releaseMaterial(taskId, materialId) {
      this._release(this._key(taskId, materialId));
    }

    releaseTask(taskId) {
      for (const [key, entry] of this.entries) {
        if (entry.taskId === taskId) this._release(key);
      }
    }

    releaseAll() {
      for (const key of [...this.entries.keys()]) this._release(key);
    }

    async _fetch(taskId, materialId) {
      const path = `/desktop/api/tasks/${encodeURIComponent(taskId)}/materials/${encodeURIComponent(materialId)}/content`;
      const response = await this.fetchImpl(`${this.apiBase}${path}`, {
        headers: { "X-Focus-Session": this.session },
      });
      if (!response.ok) throw new Error(`材料内容加载失败 (${response.status})`);
      return URL.createObjectURL(await response.blob());
    }

    _release(key) {
      const entry = this.entries.get(key);
      if (!entry) return;
      this.entries.delete(key);
      if (entry.url) URL.revokeObjectURL(entry.url);
    }

    _key(taskId, materialId) {
      return `${taskId}:${materialId}`;
    }
  }

  global.MaterialContentLoader = MaterialContentLoader;
})(window);
