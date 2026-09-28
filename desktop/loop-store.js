/*
 * 本文件对外提供旧 Loop 视图所需的兼容读模型 Store。
 * 输入为一次性旧快照/关联数据、权威 Live projection、独立连接状态和本地控制结果；输出为现有视图可读取但不再拥有 Live 领域状态的快照。
 * 具体工作流为启动时装载兼容字段，运行中分别投影连接状态与领域生命周期；停止连接时清除旧 Live 视图，旧游标接口仅保留给回归测试和回滚路径。示例：`store.clearLive(connection)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopStore = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function create() {
    let current = Object.freeze({ snapshot: null, related: null, live: null, connection: null, events: [], cursor: 0, pendingControl: null, error: null });
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
      load(snapshot) { eventIds.clear(); return publish({ snapshot, related: null, live: null, connection: null, events: [], cursor: 0, pendingControl: null, error: null }); },
      reconcile(snapshot) { return publish({ snapshot, pendingControl: null, error: null }); },
      projectConnection(connection) { return publish({ connection }); },
      clearLive(connection) { return publish({ live: null, connection, cursor: 0 }); },
      projectLive(projection, connection = null) {
        if (!projection?.loop) return current;
        const loop = projection.loop.state;
        const mission = projection.mission?.state;
        const missionRevision = loop.active_mission_revision || loop.goal_revision;
        const deliveryEvent = Object.values(projection.directives || {}).find(item => item.state?.origin === "mission_bootstrap" && item.state?.mission_revision === missionRevision);
        const delivery = deliveryEvent ? {
          state: deliveryEvent.state.run_id ? "delivered" : deliveryEvent.state.state === "delivery_failed" ? "blocked" : "authorized",
          mission_revision: missionRevision,
          reason: deliveryEvent.state.reason || null,
          directive_id: deliveryEvent.entity_id,
          run_id: deliveryEvent.state.run_id || null,
        } : current.snapshot?.mission_delivery?.mission_revision === missionRevision
          ? current.snapshot.mission_delivery
          : { state: "pending", mission_revision: missionRevision, reason: null, directive_id: null, run_id: null };
        const snapshot = {
          ...(current.snapshot || {}),
          ...loop,
          loop_id: projection.loop_id,
          mission: mission ? { outcome: mission.outcome, boundaries: mission.boundaries || {}, completion_checks: mission.completion_checks || [] } : current.snapshot?.mission,
          active_mission_revision: loop.active_mission_revision || loop.goal_revision,
          mission_delivery: delivery,
          wait_request: globalThis.FocusLoopLiveSelectors?.selectActiveWaitRequest(projection) || null,
          projection_diagnostics: projection.diagnostics,
        };
        return publish({ snapshot, live: projection, connection, cursor: projection.last_sequence, pendingControl: null });
      },
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
