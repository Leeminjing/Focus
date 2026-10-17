/* 本文件对外提供 FocusWorkspacePatrolIntake.create 工作区输入编排。
 * 输入为同源 runtime、既有 Loop API、资源上限及目录选择回调；输出为按工作区隔离的既有输入 store、绑定、材料预览与稳定发送。
 * 工作流为在内存保存有界附件字节，显式发送时冻结草稿、按项幂等上传，再投递既有 workspace intake；重试复用原提交，回执不覆盖新草稿。
 * 空附件保持旧请求格式，文件内容与用户要求分开；本模块不启动模型或保存第二份领域状态。示例：intake.store(id).edit({content:"解释图片"}); await intake.submit(id)。
 * 部分上传后明确移除的独占材料使用原提交身份清理；曾投递且受理不确定的材料保留，服务端引用锁始终决定能否删除。
 */
(function (root, factory) {
  const api = factory(root.FocusWorkspacePatrolInputs || (typeof require === "function" ? require("./workspace-patrol-inputs.js") : null),
    root.FocusHttpResponse || (typeof require === "function" ? require("./http-response.js") : null));
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolIntake = api;
})(globalThis, function (Inputs, Http) {
  "use strict";
  const bindingKey = "focus-patrol-workspace";
  function readBinding(storage) {
    try {
      const value = JSON.parse(storage?.getItem(bindingKey) || "null");
      return typeof value?.workspace_id === "string" && typeof value?.path === "string" ? value : null;
    } catch { return null; }
  }

  function create({ runtime, api, fetchImpl = globalThis.fetch, storage = globalThis.localStorage, selectWorkspace, accessMode = "workspace-write", limits = null, identity = () => crypto.randomUUID() }) {
    const stores = new Map(), sources = new Map(), uploads = new Map(), pending = new Map();
    const submitted = new Set();
    const prefix = `${String(runtime.apiBase || "").replace(/\/$/, "")}/desktop/api`;
    const store = id => {
      if (!stores.has(id)) stores.set(id, Inputs.create(identity, accessMode));
      return stores.get(id);
    };
    async function request(path, options = {}) {
      const headers = { "X-Focus-Session": runtime.session, ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }) };
      return Http.decodeResponse(await fetchImpl(`${prefix}${path}`, { ...options, headers }), { operation: "工作区输入" });
    }
    async function resourceLimits() {
      if (!limits) limits = await request("/materials/limits");
      return limits;
    }
    function remember(workspace) {
      storage?.setItem(bindingKey, JSON.stringify(workspace));
      return workspace;
    }
    async function bind(path) {
      return request("/workspaces", { method: "POST", body: JSON.stringify({ path }) });
    }
    async function choose() {
      return selectWorkspace ? selectWorkspace() : (await request("/workspaces/select", { method: "POST" })).path;
    }
    async function workspace(id) {
      return request(`/workspaces/${encodeURIComponent(id)}`);
    }
    function bytesFor(item) {
      return item.kind === "text" ? new TextEncoder().encode(item.text) : new Uint8Array(item.bytes);
    }
    async function add(id, items) {
      const resource = await resourceLimits();
      const additions = [];
      const current = store(id).get().draft.attachments || [];
      let size = [...sources.values()].reduce((sum, bytes) => sum + bytes.byteLength, 0);
      if (current.length + items.length > Math.min(32, resource.run_material_count)) throw new Error("本次材料数量超限，请分批发送");
      for (const item of items) {
        if (item.kind !== "text" && item.kind !== "file") throw new Error("不支持的材料类型");
        const bytes = bytesFor(item);
        if (!bytes.byteLength) throw new Error("材料内容为空");
        const type = item.kind === "text" ? "text/plain" : (item.type || "application/octet-stream");
        if (bytes.byteLength > (type.startsWith("image/") ? resource.original_image_bytes : resource.general_file_bytes)) throw new Error("材料超过大小上限");
        size += bytes.byteLength;
        if (size > resource.general_file_bytes) throw new Error("未发送材料总量超限，请发送或移除已有材料");
        const localId = identity();
        additions.push({ meta: { local_id: localId, name: item.kind === "text" ? "参考文字.txt" : item.name || "附件", source_kind: item.kind === "text" ? "text" : type.startsWith("image/") ? "image" : "file", type, size: bytes.byteLength }, bytes });
      }
      for (const item of additions) sources.set(item.meta.local_id, item.bytes);
      store(id).edit({ attachments: [...current, ...additions.map(item => item.meta)] });
    }
    async function remove(id, localId) {
      store(id).edit({ attachments: (store(id).get().draft.attachments || []).filter(item => item.local_id !== localId) });
      releaseUnused();
      await cleanupUploads();
    }
    function releaseUnused() {
      const used = new Set([...stores.values()].flatMap(value => {
        const model = value.get();
        return [...(model.draft.attachments || []), ...model.rows.filter(row => ["sending", "failed"].includes(row.status)).flatMap(row => row.request?.attachments || [])].map(item => item.local_id);
      }));
      for (const key of sources.keys()) if (!used.has(key)) sources.delete(key);
      const accepted = new Set([...stores.values()].flatMap(value => value.get().rows.filter(row => row.status === "accepted").map(row => row.submission_id)));
      for (const key of uploads.keys()) if (accepted.has(key.slice(0, key.lastIndexOf(":")))) uploads.delete(key);
      for (const id of accepted) submitted.delete(id);
    }
    async function cleanupUploads() {
      const edited = new Set([...stores.values()].flatMap(value => value.get().rows.filter(row => row.status === "edited").map(row => row.submission_id)));
      for (const [key, material] of uploads) {
        if (!edited.has(material.submission_id) || submitted.has(material.submission_id) || sources.has(material.local_id)) continue;
        try {
          await request(`/workspaces/${encodeURIComponent(material.workspace_id)}/materials/${encodeURIComponent(material.material_id)}?${new URLSearchParams({ submission_id: material.submission_id })}`, { method: "DELETE" });
          uploads.delete(key);
        } catch (error) {
          if ([404, 409].includes(error.status)) uploads.delete(key);
          else throw error;
        }
      }
    }
    function history(id, page, older = false, initialize = false) {
      store(id).history(page, older, initialize);
      releaseUnused();
    }
    function editFailed(id, submissionId) { store(id).editFailed(submissionId); releaseUnused(); }
    function claimUnbound(id) {
      const unbound = stores.get(null);
      if (!unbound) return;
      const draft = unbound.get().draft;
      if (!draft.content && !draft.attachments?.length && !draft.budget_preset) return;
      const target = store(id).get().draft;
      store(id).edit({ content: [target.content, draft.content].filter(Boolean).join("\n"), attachments: [...(target.attachments || []), ...(draft.attachments || [])], ...(draft.budget_preset ? { budget_preset: draft.budget_preset } : {}) });
      unbound.edit({ content: "", attachments: [], budget_preset: null });
    }
    async function prepare(id, row) {
      const result = [];
      for (const [index, item] of (row.request.attachments || []).entries()) {
        if (!item.local_id) { result.push(item); continue; }
        const key = `${row.submission_id}:${index}`;
        let uploaded = uploads.get(key);
        if (!uploaded) {
          const bytes = sources.get(item.local_id);
          if (!bytes) throw new Error("预览内容已不可用，请重新添加材料");
          const data = new FormData();
          data.append("file", new Blob([bytes], { type: item.type }), item.name);
          uploaded = await request(`/workspaces/${encodeURIComponent(id)}/materials/upload?${new URLSearchParams({ submission_id: row.submission_id, item_id: String(index) })}`, { method: "POST", body: data });
          uploads.set(key, { ...uploaded, local_id: item.local_id, workspace_id: id, submission_id: row.submission_id });
        }
        result.push({ material_id: uploaded.material_id, sha256: uploaded.sha256 || uploaded.digest, source_kind: item.source_kind, note: item.note || "" });
      }
      return { ...row.request, ...(result.length ? { attachments: result } : {}) };
    }
    function send(id, row) {
      if (!id) return Promise.reject(new Error("请先绑定工作区"));
      if (pending.has(row.submission_id)) return pending.get(row.submission_id);
      const operation = (async () => {
        try {
          const payload = await prepare(id, row);
          submitted.add(row.submission_id);
          const receipt = await api.submitWorkspaceInput(id, payload);
          store(id).accepted(row.submission_id, receipt);
          releaseUnused();
          return receipt;
        } catch (failure) {
          const accepted = store(id).get().rows.find(item => item.submission_id === row.submission_id && item.status === "accepted");
          if (accepted) { releaseUnused(); return accepted; }
          if ([400, 403, 404, 409, 413, 415, 422].includes(failure.status)) submitted.delete(row.submission_id);
          store(id).failed(row.submission_id, failure); throw failure;
        }
        finally { pending.delete(row.submission_id); }
      })();
      pending.set(row.submission_id, operation);
      return operation;
    }
    async function submit(id, visibleContext) {
      if (!id) throw new Error("请先绑定工作区");
      return send(id, store(id).submit(visibleContext));
    }
    async function retry(id, submissionId) {
      if (pending.has(submissionId)) return pending.get(submissionId);
      const row = store(id).retry(submissionId);
      return row ? send(id, row) : null;
    }
    function hasUnfinished() {
      return submitted.size > 0 || [...stores.values()].some(value => {
        const model = value.get();
        return Boolean(model.draft.content || model.draft.attachments?.length || model.rows.some(row => ["sending", "failed"].includes(row.status)));
      });
    }
    return Object.freeze({ store, add, remove, submit, send, retry, bind, choose, workspace, remember, claimUnbound, resourceLimits, history, editFailed,
      readBinding: () => readBinding(storage), hasUnfinished, isSending: () => pending.size > 0,
      source: localId => sources.get(localId), request });
  }
  return Object.freeze({ create, readBinding });
});
