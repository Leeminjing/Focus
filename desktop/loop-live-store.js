/*
 * 本文件对外提供单一权威 Live Loop projection Store。
 * 输入为完整 snapshot、canonical event、连接状态和局部界面偏好；输出为可订阅的领域投影与保持选择/筛选/草稿/滚动锚点的 UI 状态。
 * 具体工作流为 snapshot 原子替换、事件纯归约、缺口时冻结最后一致视图，并将界面状态与领域状态分离；示例：`store.replaceSnapshot(snapshot)`。
 */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports ? require("./loop-live-schema.js") : root.FocusLoopLiveSchema,
    typeof module === "object" && module.exports ? require("./loop-live-reducer.js") : root.FocusLoopLiveReducer,
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveStore = api;
})(typeof globalThis === "object" ? globalThis : this, function (Schema, Reducer) {
  "use strict";

  function create() {
    let current = Object.freeze({
      projection: null,
      connection: Object.freeze({ status: "idle", error: null, retry_count: 0 }),
      ui: Object.freeze({ selected_context_id: null, filters: Object.freeze({ fact_kind: "all", fact_status: "all", fact_scope: "current" }), composer_draft: "", scroll_anchors: Object.freeze({}), expanded_ids: Object.freeze([]) }),
    });
    const listeners = new Set();
    const publish = patch => {
      current = Object.freeze({ ...current, ...patch });
      listeners.forEach(listener => listener(current));
      return current;
    };
    return Object.freeze({
      get: () => current,
      subscribe(listener) { listeners.add(listener); listener(current); return () => listeners.delete(listener); },
      reset() { return publish({ projection: null, connection: Object.freeze({ status: "idle", error: null, retry_count: 0 }) }); },
      replaceSnapshot(snapshot) {
        const projection = Schema.validateSnapshot(snapshot);
        const selected = current.ui.selected_context_id;
        const selected_context_id = selected && projection.contexts[selected] ? selected : Object.keys(projection.contexts)[0] || null;
        return publish({ projection, ui: Object.freeze({ ...current.ui, selected_context_id }) });
      },
      applyEvent(event) { return publish({ projection: Reducer.reduce(current.projection, event) }); },
      applyEvents(events) { return publish({ projection: Reducer.reduceBatch(current.projection, events) }); },
      setConnection(status, patch = {}) { return publish({ connection: Object.freeze({ ...current.connection, ...patch, status }) }); },
      setUi(patch) { return publish({ ui: Object.freeze({ ...current.ui, ...patch }) }); },
    });
  }

  return Object.freeze({ create });
});
