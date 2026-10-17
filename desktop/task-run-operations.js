/**
 * 本文件对外提供 FocusTaskRunOperations 任务级异步操作 store。
 * 输入为 task id、request id、Run 或耐久消息受理回执、stream transition 或错误；输出为按 task 隔离的只读状态和受理凭证，凭证不宣称后续执行阶段。
 * 具体工作流为 begin 建立请求所有权，accept/fail 仅处理匹配请求，只有带 Run 身份且非 direct_message 的回执更新 active_run；updateRun 按 Run 身份更新，finally 清理本请求资源。
 * freeze 在投递前按任务持久化完整原请求；桌面优先使用 userData 仓库跨端口恢复，浏览器使用 localStorage。
 * recovery 迁移当前 origin 可见的旧凭证；finish 不删除未确认请求，只有受理回执或显式编辑后的新提交才能替换它。
 * 示例：`const operations = FocusTaskRunOperations.create(); operations.begin("task-1", "request-1")`。
 */

(function initTaskRunOperations(global) {
  function empty(taskId) {
    return { task_id: taskId, submission: null, admission: null, active_run: null, streams: {}, error: null };
  }

  function clone(value) {
    return value ? JSON.parse(JSON.stringify(value)) : value;
  }

  function create({ storage = global.focusDesktop?.mainRequestStorage || global.localStorage } = {}) {
    const tasks = new Map();
    const recoveries = new Map();
    const recoveryKey = taskId => `focus-main-request:${taskId}`;
    function recovery(taskId) {
      if (!recoveries.has(taskId)) {
        const key = recoveryKey(taskId);
        let saved = storage?.getItem(key);
        // 只迁移仍可见的旧 origin；稳定仓库读写失败时上抛，不能当作没有原请求。
        const legacy = storage === global.focusDesktop?.mainRequestStorage ? global.localStorage : null;
        if (!saved && legacy) {
          saved = legacy.getItem(key);
          if (saved) storage.setItem(key, saved);
        }
        const value = saved ? JSON.parse(saved) : null;
        if (value && (value.version !== 1 || typeof value.request_id !== "string" || typeof value.body !== "string")) {
          throw new Error("普通任务恢复数据无法读取，请保留原请求后重试");
        }
        if (saved && legacy) legacy.removeItem(key);
        recoveries.set(taskId, value);
      }
      return clone(recoveries.get(taskId));
    }
    function saveRecovery(taskId, value) {
      storage?.setItem(recoveryKey(taskId), JSON.stringify(value));
      recoveries.set(taskId, value);
      return clone(value);
    }
    const stateOf = taskId => tasks.get(taskId) || empty(taskId);
    const write = (taskId, state) => tasks.set(taskId, state);
    return Object.freeze({
      recovery,
      freeze(taskId, requestId, snapshot) {
        return saveRecovery(taskId, { ...clone(snapshot), version: 1, request_id: requestId, editing: false });
      },
      editRecovery(taskId) {
        const saved = recovery(taskId);
        return saved ? saveRecovery(taskId, { ...saved, editing: true }) : null;
      },
      failRecovery(taskId, requestId, error) {
        const saved = recovery(taskId);
        if (saved?.request_id !== requestId) return null;
        return saveRecovery(taskId, { ...saved, failure: {
          uncertain: ![400, 401, 403, 404, 409, 413, 415, 422].includes(error.status),
          message: String(error.message),
        } });
      },
      forgetRecovery(taskId, requestId) {
        if (recovery(taskId)?.request_id !== requestId) return false;
        storage?.removeItem(recoveryKey(taskId));
        recoveries.set(taskId, null);
        return true;
      },
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
          admission: run?.intent_kind === "direct_message" ? clone(run) : null,
          active_run: run?.run_id && run.intent_kind !== "direct_message" ? clone(run) : current.active_run,
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
