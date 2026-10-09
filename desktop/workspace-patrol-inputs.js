/* 本文件对外提供工作区 Patrol 的输入记录与提交快照 store。
 * 输入为四类草稿、指定请求、历史页及耐久回执；输出为独立发送状态和合计最近三条记录。
 * 具体工作流为提交时冻结正文/类型/目标并立即释放该草稿，回调仅修改对应记录；重试沿用身份，
 * 新草稿、折叠和历史分页互不覆盖。示例：const row = store.submit(); await send(row.request)。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolInputs = api;
})(globalThis, function () {
  "use strict";
  const types = Object.freeze({ information: "普通信息", outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" });
  function create(identity = () => crypto.randomUUID(), accessMode = "workspace-write") {
    let draft = { content: "", input_type: "information", request_id: null, request_revision: null };
    let expanded = false;
    let order = 0;
    let cursor = null;
    const rows = new Map();
    const listeners = new Set();
    const sorted = () => [...rows.values()].sort((a, b) => {
      const time = String(b.created_at || "").localeCompare(String(a.created_at || ""));
      return time || b.order - a.order || String(b.intent_id || "").localeCompare(String(a.intent_id || ""));
    });
    const get = () => ({ draft: { ...draft }, expanded, cursor, rows: sorted(), visible: expanded ? sorted() : sorted().slice(0, 3) });
    const publish = () => listeners.forEach(listener => listener(get()));
    return Object.freeze({
      get,
      subscribe(listener) { listeners.add(listener); listener(get()); return () => listeners.delete(listener); },
      edit(patch) { draft = { ...draft, ...patch }; publish(); },
      expand(value) { expanded = value; publish(); },
      submit() {
        if (!draft.content.trim()) throw new Error("请输入非空信息");
        const request = Object.freeze({ submission_id: identity(), access_mode: accessMode, ...draft });
        const row = { request, ...request, status: "sending", error: null, order: ++order, created_at: new Date().toISOString() };
        rows.set(request.submission_id, row);
        draft = { ...draft, content: "", request_id: null, request_revision: null };
        publish();
        return row;
      },
      retry(id) { const row = rows.get(id); if (!row || row.status !== "failed") return null; row.status = "sending"; row.error = null; publish(); return row; },
      accepted(id, receipt) { const row = rows.get(id); if (!row) return; Object.assign(row, receipt, { status: "accepted", error: null }); publish(); },
      failed(id, error) { const row = rows.get(id); if (!row) return; row.status = "failed"; row.error = String(error.message || error); publish(); },
      history(page, older = false) {
        for (const item of page.items) {
          const id = item.submission_id;
          const old = rows.get(id);
          rows.set(id, { ...old, ...item, order: old?.order || 0, status: "accepted" });
        }
        if (older || cursor === null) cursor = page.next_before;
        publish();
      },
    });
  }
  return Object.freeze({ create, types });
});
