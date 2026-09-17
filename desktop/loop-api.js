/*
 * 本文件对外提供 Agent Loop HTTP 与持久事件协议入口。
 * 输入为桌面运行时、Loop 请求、控制台查询和事件游标；输出为规范化响应、分页会话、事实、压缩来源恢复与可恢复事件订阅。
 * 具体工作流为封装同源 API，Context 直接发言复用 Main Run，Patrol 意图走独立介入端口，来源恢复复用规范 quick-apply。
 * 示例：`FocusLoopApi.create(runtime)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopApi = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function create(runtime, fetchImpl = globalThis.fetch) {
    const root = `${String(runtime.apiBase || "").replace(/\/$/, "")}/desktop/api`;
    const base = `${root}/agent-loops`;
    const headers = { "Content-Type": "application/json", "X-Focus-Session": runtime.session };
    async function request(path, options = {}) {
      const response = await fetchImpl(`${base}${path}`, { ...options, headers: { ...headers, ...(options.headers || {}) } });
      const payload = await response.json();
      if (!response.ok) throw Object.assign(new Error(payload?.detail?.message || payload?.detail || "Agent Loop 请求失败"), { status: response.status, payload });
      return payload;
    }
    async function stream(loopId, after, onEvents, signal) {
      const response = await fetchImpl(`${base}/${encodeURIComponent(loopId)}/events/stream?after=${Number(after) || 0}`, { headers, signal });
      if (!response.ok || !response.body) throw new Error("Agent Loop 事件流连接失败");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let cursor = Number(after) || 0;
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const data = frame.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
          if (!data) continue;
          const event = JSON.parse(data);
          cursor = Math.max(cursor, Number(event.cursor) || 0);
          onEvents([event]);
        }
        if (chunk.done) return cursor;
      }
    }
    return Object.freeze({
      start: body => request("", { method: "POST", body: JSON.stringify(body) }),
      findByContext: contextId => request(`/by-context/${encodeURIComponent(contextId)}`),
      get: loopId => request(`/${encodeURIComponent(loopId)}`),
      control: (loopId, command) => request(`/${encodeURIComponent(loopId)}/control`, { method: "POST", body: JSON.stringify({ command }) }),
      mutateGrant: (loopId, body) => request(`/${encodeURIComponent(loopId)}/grant`, { method: "POST", body: JSON.stringify(body) }),
      override: (loopId, body) => request(`/${encodeURIComponent(loopId)}/override`, { method: "POST", body: JSON.stringify(body) }),
      intervene: (loopId, body) => request(`/${encodeURIComponent(loopId)}/interventions`, { method: "POST", body: JSON.stringify(body) }),
      console: loopId => request(`/${encodeURIComponent(loopId)}/console`),
      conversation: (loopId, contextId, options = {}) => {
        const query = new URLSearchParams();
        if (options.revisionId) query.set("revision_id", options.revisionId);
        if (options.before != null) query.set("before", String(options.before));
        if (options.limit) query.set("limit", String(options.limit));
        return request(`/${encodeURIComponent(loopId)}/contexts/${encodeURIComponent(contextId)}/conversation${query.size ? `?${query}` : ""}`, { signal: options.signal });
      },
      facts: (loopId, options = {}) => {
        const query = new URLSearchParams();
        if (options.contextId) query.set("context_id", options.contextId);
        if (options.kind) query.set("kind", options.kind);
        if (options.status) query.set("status", options.status);
        if (options.before != null) query.set("before", String(options.before));
        if (options.limit) query.set("limit", String(options.limit));
        return request(`/${encodeURIComponent(loopId)}/facts${query.size ? `?${query}` : ""}`, { signal: options.signal });
      },
      async directMessage(contextId, content) {
        const taskResponse = await fetchImpl(`${root}/tasks/${encodeURIComponent(contextId)}`, { headers });
        const task = await taskResponse.json();
        if (!taskResponse.ok) throw new Error(task?.detail || "Context 装备读取失败");
        const equipment = task?.ui_state?._main_run_equipment || {};
        const response = await fetchImpl(`${root}/tasks/${encodeURIComponent(contextId)}/main/runs`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            message: content,
            model_name: equipment.model_name || null,
            skills: Array.isArray(equipment.skills) ? equipment.skills : [],
            permissions: Array.isArray(equipment.permissions) && equipment.permissions.length ? equipment.permissions : ["read", "write"],
            access_mode: equipment.access_mode || "workspace",
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || "Context 消息发送失败");
        return payload;
      },
      async restoreCompression(contextId, messageId) {
        const response = await fetchImpl(`${root}/compression/quick-apply`, {
          method: "POST",
          headers,
          body: JSON.stringify({ task_id: contextId, ranges: [{ source_ids: [messageId], restore: true }], scrub_terms: [] }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || "压缩来源恢复失败");
        return payload;
      },
      events: (loopId, after = 0) => request(`/${encodeURIComponent(loopId)}/events?after=${Number(after) || 0}`),
      revision: async revisionId => {
        const response = await fetchImpl(`${root}/context-revisions/${encodeURIComponent(revisionId)}`, { headers });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.detail || "Context revision 请求失败");
        return payload;
      },
      stream,
      async related(snapshot) {
        const requestRoot = async path => {
          const response = await fetchImpl(`${root}${path}`, { headers });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload?.detail || "Agent Loop 关联数据请求失败");
          return payload;
        };
        const [portfolio, evolution, tree, audit, slots] = await Promise.all([
          snapshot.program_id ? requestRoot(`/curation-programs/${encodeURIComponent(snapshot.program_id)}`) : null,
          requestRoot(`/workspaces/${encodeURIComponent(snapshot.workspace_id)}/context-evolution`),
          requestRoot(`/workspaces/${encodeURIComponent(snapshot.workspace_id)}/context-tree`),
          requestRoot(`/agent-loops/${encodeURIComponent(snapshot.loop_id)}/audit`),
          requestRoot(`/workspaces/${encodeURIComponent(snapshot.workspace_id)}/slots`),
        ]);
        return { portfolio, evolution, tree, audit, slots };
      },
    });
  }

  return Object.freeze({ create });
});
