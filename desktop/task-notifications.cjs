"use strict";

// Reconcile in the main process so hidden workbenches still receive notifications.
// Persist identities/statuses only, never task content or approval payloads.
class TaskNotifications {
  constructor({ load, save, listTasks, show, onTasks = () => {}, subscribe,
    interval = 60000, debounce = 250 }) {
    Object.assign(this, { save, listTasks, show, onTasks, subscribe, interval, debounce });
    this.generation = 0;
    this.abort = new AbortController();
    const stored = load() || {};
    this.enabled = stored.enabled !== false;
    this.seen = new Set(Array.isArray(stored.seen) ? stored.seen : []);
    this.initialized = false;
    this.busy = false;
    this.stopped = false;
    this.saved = JSON.stringify({ enabled: this.enabled, seen: [...this.seen] });
  }
  persist() {
    this.seen = new Set([...this.seen].slice(-2000));
    const value = { enabled: this.enabled, seen: [...this.seen] };
    const serialized = JSON.stringify(value);
    if (serialized !== this.saved) { this.save(value); this.saved = serialized; }
  }
  settings() { return { enabled: this.enabled }; }
  configure(enabled) {
    if (typeof enabled !== "boolean") throw new TypeError("提醒开关必须是布尔值");
    this.enabled = enabled;
    this.persist();
    return this.settings();
  }
  async poll() {
    if (this.busy || this.stopped) return;
    this.busy = true;
    const generation = this.generation;
    try {
      const tasks = await this.listTasks(this.abort.signal);
      if (this.stopped) return;
      this.onTasks(tasks);
      const events = [];
      for (const task of tasks) {
        if (task.lifecycle && task.lifecycle !== "active") continue;
        const loop = task.loop;
        const workspacePatrol = loop?.interaction_mode === "workspace_patrol";
        for (const item of task.attention || []) {
          events.push({ key: `attention:${task.task_id}:${item.id}`, priority: 0,
            title: item.kind === "access_approval" ? "需要你授权" : "需要你处理",
            body: task.title, target: { taskId: task.task_id, attentionId: item.id, loopId: item.loop_id || null,
              ...(workspacePatrol && item.loop_id === loop.loop_id ? { workspaceId: loop.workspace_id } : {}) } });
        }
        // Only the Patrol's root summary emits the shared terminal result.
        if (workspacePatrol && task.task_id !== loop.initial_context_id) continue;
        if (task.active_run || task.attention?.length || task.attention_unavailable) continue;
        if (loop && !["completed", "failed"].includes(loop.status)) continue;
        const result = loop && ["completed", "failed"].includes(loop.status)
          ? { run_id: `loop:${loop.loop_id}`, status: loop.status === "completed" ? "success" : "error" }
          : task.latest_run;
        if (!result || !["success", "error"].includes(result.status)) continue;
        const event = { key: `result:${task.task_id}:${result.run_id}:${result.status}`, priority: 1,
          title: result.status === "success" ? "任务已完成" : "任务运行失败",
          body: task.title, target: { taskId: task.task_id, loopId: loop?.loop_id || null,
            ...(workspacePatrol ? { workspaceId: loop.workspace_id } : {}) } };
        // Starting the application does not replay historical results.
        if (!this.initialized) this.seen.add(event.key);
        else events.push(event);
      }
      this.initialized = true;
      for (const event of events.sort((a, b) => a.priority - b.priority)) {
        if (this.seen.has(event.key)) continue;
        if (this.enabled) this.show(event);
        this.seen.add(event.key);
      }
      this.persist();
    } finally {
      this.busy = false;
      if (generation !== this.generation) this.schedule();
    }
  }
  invalidate() {
    this.generation += 1;
    if (!this.busy) this.schedule();
  }
  schedule() {
    if (this.stopped || this.refreshTimer) return;
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      void this.poll().catch(error => console.warn("任务状态刷新失败:", error.message));
    }, this.debounce);
    this.refreshTimer.unref?.();
  }
  start() {
    if (this.timer || this.stopped) return;
    const tick = () => this.poll().catch(error => console.warn("任务状态刷新失败:", error.message));
    void tick();
    this.unsubscribe = this.subscribe?.(() => this.invalidate());
    this.timer = setInterval(tick, this.interval);
    this.timer.unref?.();
  }
  stop() {
    this.stopped = true;
    this.abort.abort();
    this.unsubscribe?.();
    clearTimeout(this.refreshTimer);
    clearInterval(this.timer);
  }
}
module.exports = { TaskNotifications };
