/*
 * 本文件对外提供 Live projection 关闭时的旧 Loop 连接回滚适配器。
 * 输入为旧事件 API、兼容 Loop Store、Console Controller 与重绘回调；输出为单连接 SSE、低频快照对账和可停止生命周期。
 * 具体工作流为旧事件只推进游标，短暂去抖后集中刷新旧查询面；该模块不参与默认 Live 路径。示例：`legacy.start(loopId)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLegacyConnection = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function create({ api, loopStore, consoleController, onChange = () => {} }) {
    let loopId = null;
    let abort = null;
    let pollTimer = null;
    let refreshTimer = null;

    async function reconcile(target) {
      const snapshot = await api.get(target);
      loopStore.reconcile(snapshot);
      loopStore.reconcileRelated(await api.related(snapshot));
      await consoleController?.refresh();
      onChange();
    }

    function scheduleRefresh(target) {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        if (loopId === target) void reconcile(target).catch(error => loopStore.fail(error));
      }, 120);
    }

    function schedulePoll(target) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(async () => {
        if (loopId !== target) return;
        try { await reconcile(target); } catch (error) { loopStore.fail(error); }
        schedulePoll(target);
      }, 15000);
    }

    function start(target) {
      if (loopId === target && abort && !abort.signal.aborted) return;
      stop();
      loopId = target;
      abort = new AbortController();
      schedulePoll(target);
      void (async () => {
        let delay = 500;
        while (loopId === target && !abort.signal.aborted) {
          try {
            await api.stream(target, loopStore.get().cursor, events => {
              loopStore.apply(events);
              scheduleRefresh(target);
            }, abort.signal);
            delay = 500;
          } catch (error) {
            if (abort.signal.aborted || error?.name === "AbortError") return;
            loopStore.fail(error);
          }
          await new Promise(resolve => setTimeout(resolve, delay));
          delay = Math.min(delay * 2, 5000);
        }
      })();
    }

    function stop() {
      loopId = null;
      abort?.abort();
      abort = null;
      clearTimeout(pollTimer);
      clearTimeout(refreshTimer);
      pollTimer = null;
      refreshTimer = null;
    }

    return Object.freeze({ start, stop });
  }

  return Object.freeze({ create });
});
