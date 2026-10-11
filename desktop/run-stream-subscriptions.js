/* 本文件对外提供 FocusRunStreamSubscriptions.create 的共享 Run SSE 订阅入口。
 * 输入为桌面 runtime、Run ID 与只读观察回调；输出为可独立 close 的订阅句柄，frame 为 {type,id,data}，data 保留原始 JSON 字符串。
 * 工作流为同一会话/Run 共用 EventSource、按数字游标去重、最多保留 512 帧供晚到观察者顺序重放；普通重连由 EventSource 承担，最后观察者离开销毁缓存。
 * 终态关闭网络但保留现有观察者可见的终态帧；回放在 microtask 中执行，回调异常相互隔离，不触发任务、审批或执行副作用。
 * 示例：const streams = FocusRunStreamSubscriptions.create(runtime); const subscription = streams.subscribe(runId, {onFrame: frame => consume(frame)}); subscription.close()。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusRunStreamSubscriptions = api;
})(globalThis, function () {
  "use strict";
  const TYPES = ["metadata", "tokens", "reasoning", "events", "interrupt", "error", "end"];

  function create(runtime, { EventSourceImpl = globalThis.EventSource, maxFrames = 512, onObserverError = () => {} } = {}) {
    const entries = new Map();
    const limit = Math.max(1, Math.min(512, Number(maxFrames) || 512));
    const failed = error => { try { onObserverError(error); } catch {} };
    const notify = (observer, method, value) => {
      if (observer.closed) return;
      try { observer.callbacks[method]?.(value)?.catch?.(failed); }
      catch (error) { failed(error); }
    };
    function connection(entry, status) {
      if (entry.status === status) return;
      entry.status = status;
      for (const observer of entry.observers) {
        if (observer.pending) observer.status = status;
        else notify(observer, "onConnection", status);
      }
    }
    function open(runId) {
      const url = `${String(runtime.apiBase || "").replace(/\/$/, "")}/desktop/api/runs/${encodeURIComponent(runId)}/stream?session=${encodeURIComponent(runtime.session)}`;
      const source = new EventSourceImpl(url);
      const entry = { source, observers: new Set(), frames: [], lastId: null, status: "connecting", ended: false };
      entries.set(runId, entry);
      source.addEventListener("open", () => { if (!entry.ended) connection(entry, "live"); });
      for (const type of TYPES) source.addEventListener(type, event => {
        if (entry.ended || entries.get(runId) !== entry) return;
        if (type === "error" && !event.data) { connection(entry, "reconnecting"); return; }
        const id = String(event.lastEventId || "");
        const sequence = /^\d+$/.test(id) ? Number(id) : null;
        if (sequence !== null && entry.lastId !== null && sequence <= entry.lastId) return;
        if (sequence !== null) entry.lastId = sequence;
        const frame = Object.freeze({ type, id, data: event.data || "" });
        entry.frames.push(frame);
        if (entry.frames.length > limit) entry.frames.splice(0, entry.frames.length - limit);
        if (type === "end") { entry.ended = true; source.close(); }
        else connection(entry, "live");
        for (const observer of [...entry.observers]) {
          if (observer.pending) { observer.queue.push(frame); if (observer.queue.length > limit) observer.queue.shift(); }
          else notify(observer, "onFrame", frame);
        }
        if (entry.ended) connection(entry, "ended");
      });
      return entry;
    }
    function subscribe(runId, callbacks, { replay = true } = {}) {
      if (!runId) throw new TypeError("Run 订阅需要明确 runId");
      const entry = entries.get(runId) || open(runId);
      const observer = { callbacks, pending: true, closed: false, queue: replay ? entry.frames.slice() : [], status: entry.status };
      entry.observers.add(observer);
      queueMicrotask(() => {
        if (observer.closed) return;
        notify(observer, "onConnection", observer.status);
        while (observer.queue.length && !observer.closed) notify(observer, "onFrame", observer.queue.shift());
        observer.pending = false;
      });
      return Object.freeze({ close() {
        if (observer.closed) return;
        observer.closed = true;
        observer.queue.length = 0;
        entry.observers.delete(observer);
        if (!entry.observers.size) { entry.source.close(); entry.frames.length = 0; entries.delete(runId); }
      } });
    }
    return Object.freeze({ subscribe });
  }
  return Object.freeze({ create });
});
