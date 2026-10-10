/*
 * 本文件对外提供 Agent Loop HTTP、Live Snapshot 与可恢复事件协议入口。
 * 输入为桌面运行时、Loop 请求、控制台查询、sequence 游标与 Live 取消信号；输出为共享响应解码后的规范化结果、分页会话、事实、显式沿用当前 Mission 的等待恢复、压缩来源恢复与单路 Live 订阅。
 * 具体工作流为封装同源 API，所有普通响应先经纯 HTTP decoder 保留 JSON/文本失败因果，再做 Loop identity 格式化；Live 通道解析 canonical SSE 与重同步控制帧，Context 直接发言复用 Main Run。
 * 示例：`FocusLoopApi.create(runtime)`。
 * 直接消息仅发送原文和稳定请求身份，服务端沿用目标 Context 装备，不读取 UI 缓存补默认值。
 * 工作区统一受理、完整用户历史、显式后继、已提交 Lineage 与精确 Observation 分页使用相同会话头与 decoder；当前进度由 Live 投影提供，历史进度按精确身份读取，事实验证状态与业务结果分别查询。
 */
(function (root, factory) {
  const responseApi = root?.FocusHttpResponse || (typeof require === "function" ? require("./http-response.js") : null);
  const api = factory(responseApi);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopApi = api;
})(typeof globalThis === "object" ? globalThis : this, function (HttpResponse) {
  "use strict";

  function create(runtime, fetchImpl = globalThis.fetch) {
    const root = `${String(runtime.apiBase || "").replace(/\/$/, "")}/desktop/api`;
    const base = `${root}/agent-loops`;
    const headers = { "Content-Type": "application/json", "X-Focus-Session": runtime.session };
    const errorMessage = detail => {
      if (typeof detail === "string") return detail;
      if (!detail || typeof detail !== "object") return "Agent Loop 请求失败";
      const eligibility = detail.eligibility || {};
      const identity = [eligibility.candidate_run_id && `Run ${eligibility.candidate_run_id}`, eligibility.predecessor_loop_id && `Loop ${eligibility.predecessor_loop_id}`].filter(Boolean).join(" · ");
      return [detail.message || detail.code || "Agent Loop 请求失败", identity].filter(Boolean).join("：");
    };
    async function decode(response, operation) {
      try {
        return await HttpResponse.decodeResponse(response, { operation });
      } catch (error) {
        if (error instanceof HttpResponse.DesktopApiError && error.detail != null) {
          const message = errorMessage(error.detail);
          if (message !== "Agent Loop 请求失败") error.message = `${message}（HTTP ${error.status}）`;
        }
        throw error;
      }
    }
    async function request(path, options = {}) {
      const response = await fetchImpl(`${base}${path}`, { ...options, headers: { ...headers, ...(options.headers || {}) } });
      return decode(response, `Agent Loop ${options.method || "GET"} ${path || "/"}`);
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
    async function liveStream(loopId, afterSequence, onFrame, signal) {
      const response = await fetchImpl(`${base}/${encodeURIComponent(loopId)}/live/stream?after_sequence=${Number(afterSequence) || 0}`, { headers, signal });
      if (!response.ok) return decode(response, `Agent Loop GET /${loopId}/live/stream`);
      if (!response.body) throw new Error("Live Loop 事件流连接失败");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const chunk = await reader.read();
          buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done }).replace(/\r\n/g, "\n");
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const data = frame.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
            if (!data) continue;
            const payload = JSON.parse(data);
            if (["snapshot_required", "resync_required"].includes(payload?.status)) {
              onFrame(payload);
              return { resync: true, reason: payload.status };
            }
            onFrame(payload);
          }
          if (chunk.done) return { resync: false };
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
    }
    return Object.freeze({
      restartWorkspacePatrol: workspaceId => request(`/workspace/${encodeURIComponent(workspaceId)}/restart`, { method: "POST" }),
      workspacePatrol: workspaceId => request(`/workspace/${encodeURIComponent(workspaceId)}`),
      submitWorkspaceInput: (workspaceId, body) => request(`/workspace/${encodeURIComponent(workspaceId)}/inputs`, { method: "POST", body: JSON.stringify(body) }),
      workspaceInputs: (workspaceId, before = null) => request(`/workspace/${encodeURIComponent(workspaceId)}/inputs${before ? `?before=${encodeURIComponent(before)}` : ""}`),
      committedLineage: (loopId, signal) => request(`/${encodeURIComponent(loopId)}/lineage`, { signal }),
      start: body => request("", { method: "POST", body: JSON.stringify(body) }),
      activationEligibility: contextId => request(`/activation-eligibility/by-context/${encodeURIComponent(contextId)}`),
      findByContext: contextId => request(`/by-context/${encodeURIComponent(contextId)}`),
      get: loopId => request(`/${encodeURIComponent(loopId)}`),
      waitRequest: loopId => request(`/${encodeURIComponent(loopId)}/wait-request`),
      resolveWait: (loopId, requestId, body) => request(`/${encodeURIComponent(loopId)}/wait-requests/${encodeURIComponent(requestId)}/responses`, { method: "POST", body: JSON.stringify(body) }),
      resumeWithCurrentMission: (loopId, requestId, body) => request(`/${encodeURIComponent(loopId)}/wait-requests/${encodeURIComponent(requestId)}/resume-with-current-mission`, { method: "POST", body: JSON.stringify(body) }),
      control: (loopId, command) => request(`/${encodeURIComponent(loopId)}/control`, { method: "POST", body: JSON.stringify({ command }) }),
      mutateGrant: (loopId, body) => request(`/${encodeURIComponent(loopId)}/grant`, { method: "POST", body: JSON.stringify(body) }),
      override: (loopId, body) => request(`/${encodeURIComponent(loopId)}/override`, { method: "POST", body: JSON.stringify(body) }),
      reviseMission: (loopId, mission) => request(`/${encodeURIComponent(loopId)}/missions`, { method: "POST", body: JSON.stringify({ confirmation: "activate", mission }) }),
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
        if (options.outcomeStatus) query.set("outcome_status", options.outcomeStatus);
        if (options.before != null) query.set("before", String(options.before));
        if (options.limit) query.set("limit", String(options.limit));
        return request(`/${encodeURIComponent(loopId)}/facts${query.size ? `?${query}` : ""}`, { signal: options.signal });
      },
      observation: (loopId, observationId, options = {}) => {
        const query = new URLSearchParams();
        if (options.section) query.set("section", options.section);
        if (options.cursor) query.set("cursor", options.cursor);
        if (options.limit) query.set("limit", String(options.limit));
        return request(`/${encodeURIComponent(loopId)}/observations/${encodeURIComponent(observationId)}${query.size ? `?${query}` : ""}`, { signal: options.signal });
      },
      taskProgress: (loopId, progressId, signal) => request(`/${encodeURIComponent(loopId)}/task-progress${progressId ? `?progress_id=${encodeURIComponent(progressId)}` : ""}`, { signal }),
      retryTaskProgress: (loopId, observationId) => request(`/${encodeURIComponent(loopId)}/task-progress/${encodeURIComponent(observationId)}/retry`, { method: "POST" }),
      factDetail: (loopId, factId, signal) => request(`/${encodeURIComponent(loopId)}/facts/${encodeURIComponent(factId)}`, { signal }),
      liveSnapshot: (loopId, signal) => request(`/${encodeURIComponent(loopId)}/live`, { signal }),
      liveStream,
      async directMessage(contextId, content, requestId = crypto.randomUUID()) {
        const response = await fetchImpl(`${root}/tasks/${encodeURIComponent(contextId)}/main/runs`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            message: content,
            idempotency_key: requestId,
          }),
        });
        return decode(response, "发送 Context 消息");
      },
      async restoreCompression(contextId, messageId) {
        const response = await fetchImpl(`${root}/compression/quick-apply`, {
          method: "POST",
          headers,
          body: JSON.stringify({ task_id: contextId, ranges: [{ source_ids: [messageId], restore: true }], scrub_terms: [] }),
        });
        return decode(response, "恢复压缩来源");
      },
      events: (loopId, after = 0) => request(`/${encodeURIComponent(loopId)}/events?after=${Number(after) || 0}`),
      revisions: async (contextId, signal) => {
        const response = await fetchImpl(`${root}/contexts/${encodeURIComponent(contextId)}/revisions`, { headers, signal });
        return decode(response, "读取 Context 版本列表");
      },
      revision: async (revisionId, signal) => {
        const response = await fetchImpl(`${root}/context-revisions/${encodeURIComponent(revisionId)}`, { headers, signal });
        return decode(response, "读取 Context revision");
      },
      stream,
      async related(snapshot) {
        const requestRoot = async path => {
          const response = await fetchImpl(`${root}${path}`, { headers });
          return decode(response, `读取 Agent Loop 关联数据 ${path}`);
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
