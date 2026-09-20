/*
 * 本文件对外提供 Live Loop 单连接控制器。
 * 输入为 Live API、权威 projection Store、Loop identity 与取消信号；输出为 snapshot 加载、游标续传、退避重连和原子重同步后的连接状态。
 * 具体工作流为先读取 snapshot，再从 last_sequence 建立唯一 SSE；遇到 sequence 缺口或服务端重同步帧即保留旧视图并重新取 snapshot；示例：`connection.start(loopId)`。
 */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports ? require("./loop-live-reducer.js") : root.FocusLoopLiveReducer,
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveConnection = api;
})(typeof globalThis === "object" ? globalThis : this, function (Reducer) {
  "use strict";

  function create({ api, store, wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)), maxBackoff = 5000 }) {
    let generation = 0;
    let abort = null;
    let running = null;

    async function synchronize(loopId, token) {
      store.setConnection("syncing", { error: null });
      const snapshot = await api.liveSnapshot(loopId);
      if (token !== generation) return false;
      store.replaceSnapshot(snapshot);
      store.setConnection("connecting", { error: null, retry_count: 0 });
      return true;
    }

    async function run(loopId, token, controller) {
      let delay = 250;
      if (!await synchronize(loopId, token)) return;
      while (token === generation && !controller.signal.aborted) {
        try {
          store.setConnection("live", { error: null });
          const outcome = await api.liveStream(loopId, store.get().projection.last_sequence, frame => {
            if (token !== generation || controller.signal.aborted) return;
            if (["snapshot_required", "resync_required"].includes(frame?.status)) throw new Reducer.SequenceGapError(store.get().projection.last_sequence + 1, frame.last_sequence || -1);
            store.applyEvent(frame);
          }, controller.signal);
          if (outcome?.resync) {
            if (!await synchronize(loopId, token)) return;
          }
          delay = 250;
        } catch (error) {
          if (controller.signal.aborted || error?.name === "AbortError" || token !== generation) return;
          if (error instanceof Reducer.SequenceGapError) {
            store.setConnection("resyncing", { error: error.message });
            if (!await synchronize(loopId, token)) return;
            delay = 250;
            continue;
          }
          const retries = store.get().connection.retry_count + 1;
          store.setConnection("reconnecting", { error: String(error?.message || error), retry_count: retries });
          await wait(delay);
          delay = Math.min(delay * 2, maxBackoff);
        }
      }
    }

    function start(loopId) {
      if (running?.loopId === loopId && abort && !abort.signal.aborted) return running.promise;
      stop();
      const token = generation;
      abort = new AbortController();
      const promise = run(loopId, token, abort).finally(() => {
        if (running?.token === token) running = null;
      });
      running = Object.freeze({ loopId, token, promise });
      return promise;
    }

    function stop() {
      generation += 1;
      abort?.abort();
      abort = null;
      running = null;
      store.setConnection("idle", { error: null, retry_count: 0 });
    }

    return Object.freeze({ start, stop, isRunning: loopId => running?.loopId === loopId && !abort?.signal.aborted });
  }

  return Object.freeze({ create });
});
