/* 本文件对外提供 FocusWorkspacePatrolFeedback 的逐输入处置、状态与已提交回答投影。
 * 输入为既有 Live 投影、连接和输入记录，或固定 revision 的会话；输出为真实状态和带 Context/revision 来源的回答卡。
 * 工作流为区分受理、执行和完成，按已提交版本查询最终 assistant 正文，忽略工具调用与推理；不把工作区结果猜测归属到某条输入。
 * createReader(api) 为单个面板保留最多 48 个不可变版本的正文（包括空回答），不缓存失败或取消的读取及可变标题。
 * entries + read 支持逐卡片读取与单项重试；results 保留批量调用兼容。本模块不创建 Loop、Run 或任务。
 * inputResult 只展示该输入的已提交说明；停止状态来自执行器结算。refreshInputs 同时更新较早的已加载输入，保留历史游标。
 * recovery 从停止/失败状态和已保存的待处理输入生成当前入口提示，不把受理视为恢复或处理完成。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolFeedback = api;
})(globalThis, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const dispositions = { discussion: "已用于讨论", decision: "已纳入决策", reference: "已作为参考", deferred: "已延期" };
  const cancellations = { cancel_requested: "已请求停止 · 等待清理", stopped: "已停止", already_terminal: "请求时已结束", settled: "已结束", unknown: "停止状态待核实" };
  function inputResult(row) {
    if (row.status !== "accepted") return "";
    const result = row.processing_result;
    if (!result) {
      const pending = { pending: "等待 Patrol 处理", observed: "正在处理 · 已进入本轮观察", addressed: "已处理 · 暂无处理说明", superseded: "已被替代" };
      return `<p class="patrol-input-result" data-patrol-processing-state="${escape(row.processing_state || "pending")}">处理结果 · ${pending[row.processing_state] || "等待 Patrol 处理"}</p>`;
    }
    const related = result.related_cancellations || [];
    const execution = related.map(item => cancellations[item.state] || cancellations.unknown);
    const evidence = result.evidence || [];
    const sections = { outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" };
    const operations = { supplement: "补充", revise: "修订", remove: "移除" };
    return `<details class="patrol-input-result" data-patrol-input-result><summary><strong>处理结果 · ${escape(dispositions[result.disposition] || "已处理")}</strong><span>${escape(result.explanation)}</span>${execution.length ? `<small>同次决策：${escape([...new Set(execution)].join("；"))}</small>` : ""}</summary>
      <div class="patrol-input-evidence"><h4>决策依据</h4><p>${escape(result.rationale || "未记录额外决策说明")}</p>
      <dl><dt>Mission 版本</dt><dd>${escape(result.mission_revision ?? "未记录")}</dd><dt>决策</dt><dd>${escape(result.decision_id)}</dd><dt>工作轮次</dt><dd>${escape(result.round_id)}</dd><dt>冻结观察</dt><dd>${escape(result.observation_id || "未记录")}${result.observation_id ? ` · 版本 ${escape(result.observation_generation ?? "未记录")}` : ""}</dd><dt>处理时间</dt><dd>${escape(result.applied_at)}</dd></dl>
      ${(result.changes || []).length ? `<h4>这条输入对 Mission 的修改</h4><ul>${result.changes.map(item => `<li>${escape(operations[item.operation || "supplement"] || item.operation)}${escape(sections[item.section] || item.section)}：${escape(item.quote)}</li>`).join("")}</ul>` : ""}
      <h4>事实来源（本次决策）</h4>${evidence.length ? evidence.map(item => `<pre>${escape(JSON.stringify(item, null, 2))}</pre>`).join("") : "<p>本次决策未附加事实引用；输入原文见上方。</p>"}
      ${related.length ? `<h4>同次决策的停止操作</h4><ul>${related.map(item => `<li><strong>${escape(cancellations[item.state] || cancellations.unknown)}</strong><p>${escape(item.reason)}</p><small>${escape(({ run: "执行", directive: "工作指令", worker: "后台工作" })[item.target_kind] || item.target_kind)} · ${escape(item.target_id)}</small></li>`).join("")}</ul>` : ""}</div></details>`;
  }
  // 历史刷新只替换内容；保留正在阅读的处理详情及键盘焦点。
  function preserveInputResults(host) {
    const open = new Set([...host.querySelectorAll("details[data-patrol-input-result][open]")].map(node => node.closest("[data-submission-id]")?.dataset.submissionId));
    const active = host.ownerDocument?.activeElement;
    const focused = active?.matches("details[data-patrol-input-result] > summary") ? active.closest("[data-submission-id]")?.dataset.submissionId : null;
    return () => {
      for (const node of host.querySelectorAll("details[data-patrol-input-result]")) {
        const id = node.closest("[data-submission-id]")?.dataset.submissionId;
        node.open = open.has(id);
        if (id === focused) node.querySelector("summary").focus({ preventScroll: true });
      }
    };
  }
  function inputSignature(projection) {
    return JSON.stringify([projection.interventions, projection.loop, projection.round,
      projection.runs, projection.directives, projection.workers]);
  }
  async function refreshInputs(api, workspaceId, model) {
    const page = await api.workspaceInputs(workspaceId);
    const latest = new Set(page.items.map(row => row.intent_id));
    const older = model.rows.map(row => row.intent_id).filter(id => id && !latest.has(id));
    const pages = [];
    for (let offset = 0; offset < older.length; offset += 100) {
      pages.push(await api.workspaceInputs(workspaceId, null, { intentIds: older.slice(offset, offset + 100) }));
    }
    return { ...page, items: [...page.items, ...pages.flatMap(item => item.items)] };
  }
  function recovery(projection, model) {
    const status = projection?.loop?.state?.status;
    if (!["stopped", "failed"].includes(status)) return null;
    const pending = model?.rows?.some(row => row.status === "accepted" && !row.processing_result
      && [undefined, "pending", "observed"].includes(row.processing_state));
    return pending ? { text: "这条输入已保存，重新启用后处理。", label: "重新启用并处理" }
      : { text: status === "stopped" ? "工作已停止，重新启用后继续。" : "工作执行失败，重新启用后继续。", label: "重新启用" };
  }
  function status(projection, connection, model) {
    if (["unavailable", "reconnecting", "resyncing"].includes(connection?.status)) return { state: "disconnected", text: "工作状态连接中断 · 草稿已保留" };
    const latest = model?.rows?.[0];
    if (latest?.status === "failed") return { state: "failed", text: "发送未确认 · 可按原身份重试" };
    if (latest?.status === "sending") return { state: "sending", text: "正在发送 · 等待受理回执" };
    const loop = projection?.loop?.state;
    if (latest?.status === "accepted" && (!projection || (latest.accepted_sequence != null && (projection.loop?.updated_sequence || 0) < latest.accepted_sequence))) return { state: "accepted", text: "已受理 · 等待工作状态更新" };
    const labels = { running: "正在工作", waiting_user: "等待你的处理", paused: "工作已暂停", stopped: "工作已停止", failed: "工作遇到问题", completed: "当前工作已完成" };
    if (loop?.status === "waiting_user" && loop.waiting_reason === "awaiting_input") return { state: "idle", text: "准备接收新输入" };
    if (labels[loop?.status]) return { state: loop.status, text: labels[loop.status] };
    return latest?.status === "accepted" ? { state: "accepted", text: "已受理 · 等待工作状态更新" } : { state: "idle", text: "准备好了 · 拖入材料或写下问题" };
  }
  function answer(conversation) {
    for (const item of [...(conversation.messages || [])].reverse()) {
      const message = item.message || {}, role = message.role || message.type;
      if (!["assistant", "ai"].includes(role) || message.tool_calls?.length || message.additional_kwargs?.tool_calls?.length) continue;
      const text = typeof message.content === "string" ? message.content : (Array.isArray(message.content) ? message.content.filter(block => ["text", "output_text"].includes(block.type)).map(block => block.text || "").join("\n") : "");
      if (text.trim()) return { text, message_id: message.id || null,
        ...(item.file_context ? { file_context: item.file_context } : {}) };
    }
    return null;
  }
  function revisions(projection) {
    return Object.values(projection?.contexts || {}).filter(entity => entity.state.current_revision_id)
      .sort((a, b) => b.updated_sequence - a.updated_sequence).slice(0, 6);
  }
  function signature(projection) { return JSON.stringify(revisions(projection).map(item => [item.entity_id, item.state.current_revision_id, item.state.title, item.state.description])); }
  function entries(projection) {
    return revisions(projection).map(entity => ({ context_id: entity.entity_id, revision_id: entity.state.current_revision_id,
      title: entity.state.title || entity.state.description || "已提交回答" }));
  }
  async function read(api, loopId, base, signal, cache) {
    signal?.throwIfAborted();
    const key = JSON.stringify([loopId, base.context_id, base.revision_id]);
    try {
      let content;
      if (cache?.has(key)) {
        content = cache.get(key);
        cache.delete(key); cache.set(key, content);
      } else {
        const conversation = await api.conversation(loopId, base.context_id, { revisionId: base.revision_id, limit: 30, signal });
        signal?.throwIfAborted();
        content = answer(conversation);
        if (cache) {
          cache.set(key, content);
          if (cache.size > 48) cache.delete(cache.keys().next().value);
        }
      }
      return content ? { ...base, ...content } : null;
    } catch (error) { signal?.throwIfAborted(); if (error.name === "AbortError") throw error; return { ...base, error: "回答读取失败，请重试读取" }; }
  }
  async function results(api, projection, signal, cache) {
    signal?.throwIfAborted();
    return (await Promise.all(entries(projection).map(base => read(api, projection.loop_id, base, signal, cache)))).filter(Boolean);
  }
  function createReader(api) {
    const cache = new Map();
    return Object.freeze({ entries, read: (loopId, base, signal) => read(api, loopId, base, signal, cache),
      results: (projection, signal) => results(api, projection, signal, cache) });
  }
  return Object.freeze({ status, recovery, answer, signature, results, createReader, inputResult, preserveInputResults, inputSignature, refreshInputs });
});
