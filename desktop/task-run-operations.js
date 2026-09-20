/**
 * 本文件对外提供 FocusTaskRunOperations 任务级异步操作 store。
 * 输入为 task id、request id、Run payload、stream transition 或错误；输出为按 task 隔离的只读状态快照。
 * 具体工作流为 begin 建立请求所有权，accept/fail 仅处理匹配请求，updateRun 按 Run 身份更新，finally 清理本请求资源。
 * 示例：`const operations = FocusTaskRunOperations.create(); operations.begin("task-1", "request-1")`。
 */

(function initTaskRunOperations(global) {
  function empty(taskId) {
    return { task_id: taskId, submission: null, active_run: null, streams: {}, error: null };
  }

  function clone(value) {
    return value ? JSON.parse(JSON.stringify(value)) : value;
  }

  function create() {
    const tasks = new Map();
    const stateOf = taskId => tasks.get(taskId) || empty(taskId);
    const write = (taskId, state) => tasks.set(taskId, state);
    return Object.freeze({
      get(taskId) {
        return clone(stateOf(taskId));
      },
      begin(taskId, requestId, controller = null) {
        const current = stateOf(taskId);
        const submission = { request_id: requestId, status: "pending", controller };
        write(taskId, { ...current, submission, error: null });
        return clone(submission);
      },
      accept(taskId, requestId, run) {
        const current = stateOf(taskId);
        if (current.submission?.request_id !== requestId) return false;
        write(taskId, {
          ...current,
          submission: { request_id: requestId, status: "accepted", controller: null },
          active_run: clone(run),
          error: null,
        });
        return true;
      },
      fail(taskId, requestId, error) {
        const current = stateOf(taskId);
        if (current.submission?.request_id !== requestId) return false;
        write(taskId, {
          ...current,
          submission: { request_id: requestId, status: "failed", controller: null },
          error: String(error),
        });
        return true;
      },
      finish(taskId, requestId) {
        const current = stateOf(taskId);
        if (current.submission?.request_id !== requestId) return false;
        write(taskId, { ...current, submission: null });
        return true;
      },
      updateRun(taskId, runId, patch) {
        const current = stateOf(taskId);
        const run = { ...(current.streams[runId] || {}), ...clone(patch), run_id: runId };
        write(taskId, {
          ...current,
          active_run: current.active_run?.run_id === runId ? { ...current.active_run, ...run } : current.active_run,
          streams: { ...current.streams, [runId]: run },
        });
        return clone(run);
      },
      abort(taskId) {
        stateOf(taskId).submission?.controller?.abort?.();
      },
    });
  }

  global.FocusTaskRunOperations = Object.freeze({ create });
})(window);
