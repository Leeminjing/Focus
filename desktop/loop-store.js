/*
 * 本文件对外提供 Agent Loop 快照与游标事件的规范化客户端状态。
 * 输入为 Loop 快照、顺序事件和本地控制结果；输出为幂等、可订阅的当前 UI 状态。
 * 具体工作流为先装载快照、按事件 ID 去重、推进游标并通知视图；示例：`FocusLoopStore.create()`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopStore = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function create() {
    let current = Object.freeze({ snapshot: null, related: null, events: [], cursor: 0, pendingControl: null, error: null });
    const listeners = new Set();
    const eventIds = new Set();
    function publish(patch) {
      current = Object.freeze({ ...current, ...patch });
      listeners.forEach(listener => listener(current));
      return current;
    }
    return Object.freeze({
      get: () => current,
      subscribe(listener) { listeners.add(listener); listener(current); return () => listeners.delete(listener); },
      load(snapshot) { eventIds.clear(); return publish({ snapshot, related: null, events: [], cursor: 0, pendingControl: null, error: null }); },
      reconcile(snapshot) { return publish({ snapshot, pendingControl: null, error: null }); },
      reconcileRelated(related) { return publish({ related, error: null }); },
      beginControl(command) { return publish({ pendingControl: command, error: null }); },
      fail(error) { return publish({ pendingControl: null, error: String(error?.message || error) }); },
      apply(events) {
        const appended = [];
        let cursor = current.cursor;
        for (const event of events || []) {
          if (!event?.event_id || eventIds.has(event.event_id) || Number(event.cursor) <= cursor) continue;
          eventIds.add(event.event_id);
          appended.push(Object.freeze({ ...event }));
          cursor = Number(event.cursor);
        }
        return appended.length ? publish({ events: [...current.events, ...appended], cursor }) : current;
      },
    });
  }

  return Object.freeze({ create });
});
