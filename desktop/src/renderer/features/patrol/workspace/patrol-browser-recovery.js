/* 浏览器 Patrol 完整恢复仓库：IndexedDB 保存快照和无路径字节，开放编辑前完成读取。
 * 附件先提交、快照后提交；写入串行执行，提交快照时清理不再引用的材料。
 * 存储不可用、额度不足和缺失材料均向调用者报错，禁止静默退回内存发送。
 * 卸载时以 localStorage 同步记录快照检查点（不复制附件），下次启动先将它和既有字节原子提交。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusPatrolBrowserRecovery = api;
})(globalThis, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const request = value => new Promise((resolve, reject) => { value.onsuccess = () => resolve(value.result); value.onerror = () => reject(value.error); });
  function transaction(db, mode, action) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["state", "sources"], mode, mode === "readwrite" ? { durability: "strict" } : {});
      let result, failure;
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(failure || tx.error || new Error(localize("Patrol 恢复写入中断")));
      Promise.resolve().then(() => action(tx.objectStore("state"), tx.objectStore("sources"))).then(value => { result = value; }, error => {
        failure = error; tx.abort();
      });
    });
  }
  function validate(state) {
    if (state?.version !== 1 || !Array.isArray(state.workspaces) || !Array.isArray(state.source_ids) || state.source_ids.length > 4096) throw new Error(localize("Patrol 恢复快照格式无效"));
    if (new TextEncoder().encode(JSON.stringify(state)).byteLength > 8 * 1024 * 1024) throw new Error(localize("本地恢复记录已满，请处理未确认的发送"));
  }
  async function open(surface, { indexedDB = globalThis.indexedDB, storage = globalThis.localStorage, byteLimit = 128 * 1024 * 1024 } = {}) {
    if (!indexedDB) throw new Error(localize("浏览器不支持 Patrol 草稿恢复存储"));
    const opening = indexedDB.open(`focus-patrol-recovery-${surface}`, 1);
    opening.onupgradeneeded = () => { opening.result.createObjectStore("state"); opening.result.createObjectStore("sources"); };
    const db = await request(opening);
    db.onversionchange = () => db.close();
    const checkpointKey = `focus-patrol-checkpoint-${surface}`;
    const checkpoint = storage?.getItem(checkpointKey);
    const saved = await transaction(db, "readwrite", async (states, sources) => {
      const state = checkpoint ? JSON.parse(checkpoint) : await request(states.get("current")) || null;
      if (state) validate(state);
      const entries = [];
      let size = 0;
      for (const id of state?.source_ids || []) {
        const bytes = await request(sources.get(id));
        if (!(bytes instanceof Uint8Array)) throw new Error(localize("Patrol 恢复材料缺失"));
        size += bytes.byteLength;
        if (size > byteLimit) throw new Error(localize("恢复材料总量超限"));
        entries.push([id, bytes]);
      }
      const used = new Set(state?.source_ids || []);
      for (const id of await request(sources.getAllKeys())) if (!used.has(id)) sources.delete(id);
      if (checkpoint) states.put(state, "current");
      return { state, sources: entries };
    }).catch(error => { db.close(); throw error; });
    if (checkpoint && storage?.getItem(checkpointKey) === checkpoint) storage.removeItem(checkpointKey);
    let writes = Promise.resolve();
    const cachedSources = new Map(saved.sources);
    const uncommitted = new Set();
    function enqueue(action) { const next = writes.then(action); writes = next.catch(() => {}); return next; }
    return Object.freeze({
      read: () => structuredClone({ state: saved.state, sources: (saved.state?.source_ids || []).map(id => [id, cachedSources.get(id)]) }),
      checkpoint(state) {
        validate(state);
        if (!storage) throw new Error(localize("浏览器无法保存刷新恢复检查点"));
        storage.setItem(checkpointKey, JSON.stringify(state));
      },
      put(id, bytes) {
        if (typeof id !== "string" || !id || id.length > 160 || !(bytes instanceof Uint8Array) || !bytes.byteLength) throw new Error(localize("恢复材料无效"));
        const copy = bytes.slice();
        return enqueue(async () => {
          await transaction(db, "readwrite", async (_states, sources) => {
            const keys = await request(sources.getAllKeys()), values = await request(sources.getAll());
            const total = values.reduce((size, value, index) => size + (keys[index] === id ? 0 : value.byteLength), copy.byteLength);
            if (total > byteLimit) throw new Error(localize("本地恢复材料总量超限，请发送或移除已有材料"));
            sources.put(copy, id);
          });
          uncommitted.add(id);
          cachedSources.set(id, copy);
        });
      },
      save(state) {
        validate(state);
        const copy = structuredClone(state);
        const priorCheckpoint = storage?.getItem(checkpointKey);
        return enqueue(async () => {
          await transaction(db, "readwrite", async (states, sources) => {
            for (const id of copy.source_ids) if (!await request(sources.get(id))) throw new Error(localize("恢复材料缺失，无法保存草稿"));
            const used = new Set([...copy.source_ids, ...uncommitted]);
            for (const id of await request(sources.getAllKeys())) if (!used.has(id)) sources.delete(id);
            states.put(copy, "current");
          });
          copy.source_ids.forEach(id => uncommitted.delete(id));
          saved.state = copy;
          const used = new Set([...copy.source_ids, ...uncommitted]);
          for (const id of cachedSources.keys()) if (!used.has(id)) cachedSources.delete(id);
          if (priorCheckpoint && storage?.getItem(checkpointKey) === priorCheckpoint) storage.removeItem(checkpointKey);
        });
      },
      close: () => db.close(),
    });
  }
  return Object.freeze({ open });
});
