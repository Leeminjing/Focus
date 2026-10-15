/* 本文件对外提供工作区 Patrol 的输入记录与提交快照 store。
 * 输入为四类草稿、指定请求、历史页及耐久回执；输出为独立发送状态、全部已加载倒序记录及历史游标。
 * 具体工作流为提交时深复制并冻结正文/类型/目标与可见因果线索，立即释放该草稿，回调仅修改对应记录；重试沿用身份与原时点，
 * 新草稿与历史分页互不覆盖；首次页建立本次遍历，普通首页刷新不重启已耗尽游标。示例：store.history(page, false, true); const row = store.submit(); await send(row.request)。
 * file_refs、附件元数据与正文同时深冻结；附件字节由 intake 持有，空附件字段不进入旧请求；校验拒绝只恢复未被后续编辑替换的草稿，重试保留原版本。
 * 显式编辑失败记录时标记本地记录已转为草稿，结束旧记录的重试能力；原发送是否受理仍以服务器历史为准。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolInputs = api;
})(globalThis, function () {
  "use strict";
  const types = Object.freeze({ information: "普通信息", outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" });
  const frozenCopy = value => value && typeof value === "object"
    ? Object.freeze(Array.isArray(value) ? value.map(frozenCopy) : Object.fromEntries(Object.entries(value).map(([key, item]) => [key, frozenCopy(item)]))) : value;
  function create(identity = () => crypto.randomUUID(), accessMode = "workspace-write") {
    let draft = { content: "", input_type: "information", request_id: null, request_revision: null, file_refs: [] };
    let historyInitialized = false;
    let order = 0;
    let draftRevision = 0;
    let cursor = null;
    const rows = new Map();
    const listeners = new Set();
    const sorted = () => [...rows.values()].sort((a, b) => {
      const time = String(b.created_at || "").localeCompare(String(a.created_at || ""));
      return time || b.order - a.order || String(b.intent_id || "").localeCompare(String(a.intent_id || ""));
    });
    const get = () => ({ draft: { ...draft }, cursor, rows: sorted() });
    const publish = () => listeners.forEach(listener => listener(get()));
    return Object.freeze({
      get,
      subscribe(listener) { listeners.add(listener); listener(get()); return () => listeners.delete(listener); },
      edit(patch) { draftRevision++; draft = { ...draft, ...patch, ...(patch.file_refs ? { file_refs: frozenCopy(patch.file_refs) } : {}), ...(patch.attachments ? { attachments: frozenCopy(patch.attachments) } : {}) }; publish(); },
      submit(visibleContext = null) {
        if (!draft.content.trim() && !draft.attachments?.length) throw new Error("请输入信息或添加材料");
        const payload = { submission_id: identity(), access_mode: accessMode, ...draft, ...(visibleContext ? { visible_context: visibleContext } : {}) };
        if (!payload.attachments?.length) delete payload.attachments;
        const request = frozenCopy(payload);
        const row = { request, ...request, status: "sending", error: null, order: ++order, draftRevision: ++draftRevision, created_at: new Date().toISOString() };
        rows.set(request.submission_id, row);
        draft = { ...draft, content: "", request_id: null, request_revision: null, file_refs: [] };
        delete draft.attachments;
        publish();
        return row;
      },
      retry(id) { const row = rows.get(id); if (!row || row.status !== "failed") return null; row.status = "sending"; row.error = null; publish(); return row; },
      accepted(id, receipt) { const row = rows.get(id); if (!row) return; Object.assign(row, receipt, { status: "accepted", error: null }); publish(); },
      failed(id, error) {
        const row = rows.get(id); if (!row) return;
        if (row.status === "accepted") return;
        row.status = "failed"; row.error = String(error.message || error);
        if ((row.file_refs?.length || row.request_id) && [403, 404, 409, 422].includes(error.status) && row.draftRevision === draftRevision) {
          draft = { ...draft, content: row.content, file_refs: row.file_refs || [], ...(row.attachments?.length ? { attachments: row.attachments } : {}), input_type: row.input_type, request_id: row.request_id, request_revision: row.request_revision };
          row.status = "edited";
          draftRevision++;
        }
        publish();
      },
      editFailed(id) {
        const row = rows.get(id);
        if (!row || row.status !== "failed" || draft.content || draft.file_refs.length || draft.attachments?.length) return;
        draftRevision++;
        draft = { ...draft, content: row.content, file_refs: row.file_refs || [], ...(row.attachments?.length ? { attachments: row.attachments } : {}), input_type: row.input_type, request_id: row.request_id, request_revision: row.request_revision };
        row.status = "edited";
        publish();
      },
      history(page, older = false, initialize = false) {
        for (const item of page.items) {
          const id = item.submission_id;
          const old = rows.get(id);
          rows.set(id, { ...old, ...item, order: old?.order || 0, status: "accepted" });
        }
        if (older || initialize || !historyInitialized) cursor = page.next_before;
        historyInitialized = true;
        publish();
      },
    });
  }
  function captureVisibleContext({ projection, facts = [], previews = {} }) {
    if (!projection?.loop_id) return null;
    const refs = new Map(facts.filter(item => item.fact_id && Number.isInteger(item.revision)).map(item => [`${item.fact_id}:${item.revision}`, { fact_id: item.fact_id, revision: item.revision }]));
    return frozenCopy({ loop_id: projection.loop_id, live_sequence: projection.last_sequence ?? null,
      fact_refs: [...refs.values()].slice(0, 256),
      run_previews: Object.values(previews).filter(item => item.run_id && item.identity?.context_id && item.text).slice(0, 32).map(item => ({
        run_id: item.run_id, context_id: item.identity.context_id, message_id: item.message_id || null,
        sequence: item.last_sequence ?? null, role: item.tool_name || item.role === "Tool" ? "tool" : "assistant",
        text: Array.from(item.text).slice(-4096).join(""), truncated: Array.from(item.text).length >= 4096,
        gap: Boolean(item.notice) || ["reconnecting", "unavailable"].includes(item.connection_status),
      })),
    });
  }
  return Object.freeze({ create, types, captureVisibleContext });
});
