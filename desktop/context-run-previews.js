/* 本文件对外提供图中 Context Run 的只读预览观察器 create。
 * 输入为共享 Run 订阅、Run 身份查询与变化回调，sync 接收当前工作区、Loop 和图节点；输出为按 Context 隔离的短预览及真实终态修正。
 * 工作流为只观察当前根的 running Run，核对身份后归约真实事件，按帧合并通知；切区、换 Run 或停止时取消查询与订阅，迟到响应不生效。
 * 终态来自已核对身份的 end/metadata/Run 查询，decorate 仅修正显示而不写领域投影，同一 Run 的终态记录在节点仍存在时阻止陈旧 running 状态重新订阅。
 * 示例：observer.sync({workspaceId, loopId, nodes}); observer.stop()。
 */
(function (root, factory) {
  const api = factory(root.FocusRunningContextPreview || (typeof require === "function" ? require("./running-context-preview.js") : null));
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextRunPreviews = api;
})(globalThis, function (Preview) {
  "use strict";
  const terminal = new Set(["success", "error", "interrupted", "timeout", "cancelled", "stopped"]);

  function create({ streams, loadRun, onChange, schedule = globalThis.requestAnimationFrame, cancel = globalThis.cancelAnimationFrame }) {
    const entries = new Map();
    let scope = null, frame = null, structural = false;
    const current = entry => entries.get(entry.contextId) === entry;
    const get = () => Object.fromEntries([...entries].map(([id, entry]) => [id, entry.preview]));
    function changed(layout = false) {
      structural ||= layout;
      if (frame !== null) return;
      frame = schedule(() => {
        frame = null;
        const update = structural;
        structural = false;
        onChange(get(), update);
      });
    }
    function release(entry) { entry.abort.abort(); entry.subscription?.close(); }
    function validate(entry, run) {
      if (run?.run_id !== entry.runId || run.task_id !== entry.contextId || run.workspace_anchor?.workspace_id !== entry.owner.workspaceId
          || (run.loop_id && run.loop_id !== entry.owner.loopId) || typeof run.execution_thread_id !== "string" || !run.execution_thread_id) throw new Error("运行预览身份不匹配");
      return run;
    }
    function finish(entry, result) {
      if (!current(entry) || entry.terminal) return;
      if (result?.run_id !== entry.runId || !terminal.has(result.status)) return unavailable(entry, "运行结束状态无法确认");
      entry.terminal = result;
      entry.preview = Preview.reduce(entry.preview, { type: "end", id: "end", data: JSON.stringify(result) });
      entry.subscription?.close();
      changed(true);
    }
    function unavailable(entry, message) {
      if (!current(entry)) return;
      entry.preview = { ...Preview.connection(entry.preview, "unavailable"), notice: message };
      entry.subscription?.close();
      changed();
    }
    async function reconcile(entry) {
      if (entry.checking || entry.terminal) return;
      entry.checking = true;
      try {
        const result = await loadRun(entry.runId, entry.abort.signal);
        if (!current(entry)) return;
        const run = validate(entry, result);
        if (terminal.has(run.status)) finish(entry, run);
      } catch (error) {
        if ([401, 403, 404].includes(Number(error.status)) || error.message === "运行预览身份不匹配") unavailable(entry, "运行预览不可用，请重试");
      } finally { entry.checking = false; }
    }
    async function attach(entry, owner) {
      try {
        const run = await loadRun(entry.runId, entry.abort.signal);
        if (!current(entry)) return;
        validate(entry, run);
        entry.preview = Preview.create({ run_id: entry.runId, context_id: entry.contextId, workspace_id: owner.workspaceId, thread_id: run.execution_thread_id, message_id: run.message_id });
        if (terminal.has(run.status)) return finish(entry, run);
        entry.subscription = streams.subscribe(entry.runId, {
          onFrame(event) {
            if (!current(entry) || entry.terminal) return;
            if (event.type === "end") {
              try {
                const result = JSON.parse(event.data);
                if (result.run_id === entry.runId) finish(entry, result);
              } catch { unavailable(entry, "运行结束状态无法读取"); }
              return;
            }
            entry.preview = Preview.reduce(entry.preview, event);
            if (entry.preview.ended) {
              try { const result = JSON.parse(event.data); finish(entry, { run_id: result.run_id, ...result.data }); }
              catch { unavailable(entry, "运行结束状态无法读取"); }
            } else changed();
          },
          onConnection(status) {
            if (!current(entry) || entry.terminal) return;
            entry.preview = Preview.connection(entry.preview, status);
            changed();
            if (status === "reconnecting") void reconcile(entry);
          },
        });
        changed();
      } catch (error) {
        if (current(entry) && error.name !== "AbortError") unavailable(entry, error.message || "运行预览不可用，请重试");
      }
    }
    function sync(owner) {
      if (scope && (scope.workspaceId !== owner.workspaceId || scope.loopId !== owner.loopId)) stop();
      scope = owner;
      const runOf = node => node?.active_run?.status === "running" ? node.active_run : node?.latest_run;
      const nodes = new Map(owner.nodes.map(node => [node.context_id, node]));
      const desired = new Map(owner.nodes.filter(node => !node.historical).map(node => [node.context_id, runOf(node)]).filter(([, run]) => run?.status === "running"));
      for (const [id, entry] of entries) {
        const node = nodes.get(id), run = entry.terminal ? runOf(node) : desired.get(id);
        if (node?.historical || (run?.run_id || run?.id) !== entry.runId) { entries.delete(id); release(entry); }
      }
      for (const [contextId, run] of desired) {
        const runId = run.run_id || run.id;
        if (!runId || entries.has(contextId)) continue;
        const entry = { contextId, runId, owner: { workspaceId: owner.workspaceId, loopId: owner.loopId }, abort: new AbortController(), preview: Preview.create({ run_id: runId, context_id: contextId, workspace_id: owner.workspaceId }) };
        entries.set(contextId, entry);
        void attach(entry, owner);
      }
    }
    function decorate(manifest) {
      return { ...manifest, nodes: manifest.nodes.map(node => {
        const entry = entries.get(node.context_id);
        const run = node.active_run?.status === "running" ? node.active_run : node.latest_run;
        if (!entry?.terminal || (run?.run_id || run?.id) !== entry.runId) return node;
        return { ...node, active_run: null, latest_run: { ...run, status: entry.terminal.status } };
      }) };
    }
    function stop() {
      for (const entry of entries.values()) release(entry);
      entries.clear(); scope = null;
      if (frame !== null) cancel(frame);
      frame = null; structural = false;
    }
    function retry() {
      for (const [id, entry] of entries) if (entry.preview.status === "unavailable") { entries.delete(id); release(entry); }
      if (scope) sync(scope);
    }
    return Object.freeze({ sync, decorate, get, stop, retry });
  }
  return Object.freeze({ create });
});
