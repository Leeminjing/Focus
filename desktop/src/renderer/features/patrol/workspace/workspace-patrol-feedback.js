/* 本文件对外提供 FocusWorkspacePatrolFeedback 的逐输入处置、状态与已提交回答投影。
 * 输入为既有 Live 投影、连接和输入记录，或固定 revision 的会话；输出为真实状态和带 Context/revision 来源的回答卡。
 * 工作流为区分受理、执行和完成，按已提交版本查询最终 assistant 正文，忽略工具调用与推理；不把工作区结果猜测归属到某条输入。
 * createReader(api) 为单个面板保留最多 48 个不可变版本的正文（包括空回答），不缓存失败或取消的读取及可变标题。
 * entries + read 支持逐卡片读取与单项重试；results 保留批量调用兼容。本模块不创建 Loop、Run 或任务。
 * inputResult 只展示该输入的已提交说明；停止状态来自执行器结算。历史刷新合并受影响输入 ID，Round 提交覆盖延期说明，关联执行结算覆盖停止回执；不重读全部已加载记录。
 * recovery 从停止/失败状态和已保存的待处理输入生成当前入口提示，不把受理视为恢复或处理完成。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolFeedback = api;
})(globalThis, function () {
  "use strict";
  // 文案身份使用稳定键；每次渲染读取当前语言，领域值仅在输出 HTML 时转义。
  const defaultI18n = !globalThis.FocusI18n && typeof require === "function" ? require("../../../shared/i18n/i18n.js") : null;
  const t = (key, variables) => (globalThis.FocusI18n || defaultI18n).t(key, variables);
  const label = (keys, value, fallback = value) => Object.hasOwn(keys, value) ? t(keys[value]) : fallback;

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const dispositions = { discussion: "patrol.feedback.discussion", decision: "patrol.feedback.decision", reference: "patrol.feedback.reference", deferred: "patrol.feedback.deferred" };
  const cancellations = { cancel_requested: "patrol.feedback.cancel_requested", stopped: "patrol.feedback.stopped", already_terminal: "patrol.feedback.already_terminal", settled: "patrol.feedback.settled", unknown: "patrol.feedback.cancel_unknown" };
  function inputResult(row) {
    if (row.status !== "accepted") return "";
    const result = row.processing_result;
    if (!result) {
      const pending = { pending: "patrol.feedback.pending", observed: "patrol.feedback.observed", addressed: "patrol.feedback.addressed", superseded: "patrol.feedback.superseded" };
      return `<p class="patrol-input-result" data-patrol-processing-state="${escape(row.processing_state || "pending")}">${escape(label(pending, row.processing_state, t(pending.pending)))}</p>`;
    }
    const related = result.related_cancellations || [];
    const execution = related.map(item => label(cancellations, item.state, t(cancellations.unknown)));
    const evidence = result.evidence || [];
    const sections = { outcome: "patrol.feedback.outcome", boundary: "patrol.feedback.boundary", completion_check: "patrol.feedback.completion_check" };
    const operations = { supplement: "patrol.feedback.supplement", revise: "patrol.feedback.revise", remove: "patrol.feedback.remove" };
    const targets = { run: "patrol.feedback.run", directive: "patrol.feedback.directive", worker: "patrol.feedback.worker" };
    return `<details class="patrol-input-result" data-patrol-input-result data-patrol-disposition="${escape(result.disposition)}"><summary><strong>${escape(label(dispositions, result.disposition, t("patrol.feedback.processed")))}</strong><span>${escape(result.explanation)}</span>${execution.length ? `<small>${escape(t("patrol.feedback.execution_summary", { states: [...new Set(execution)].join(t("patrol.feedback.separator")) }))}</small>` : ""}</summary>
      <div class="patrol-input-evidence"><h4>${escape(t("patrol.feedback.rationale"))}</h4><p>${escape(result.rationale || t("patrol.feedback.no_rationale"))}</p>
      <dl><dt>${escape(t("patrol.feedback.mission_revision"))}</dt><dd data-patrol-source="mission_revision">${escape(result.mission_revision ?? t("patrol.feedback.not_recorded"))}</dd><dt>${escape(t("patrol.feedback.decision_id"))}</dt><dd data-patrol-source="decision_id">${escape(result.decision_id)}</dd><dt>${escape(t("patrol.feedback.round_id"))}</dt><dd data-patrol-source="round_id">${escape(result.round_id)}</dd><dt>${escape(t("patrol.feedback.observation"))}</dt><dd><span data-patrol-source="observation_id">${escape(result.observation_id || t("patrol.feedback.not_recorded"))}</span>${result.observation_id ? ` · <span data-patrol-source="observation_generation">${escape(t("patrol.feedback.version", { version: result.observation_generation ?? t("patrol.feedback.not_recorded") }))}</span>` : ""}</dd><dt>${escape(t("patrol.feedback.applied_at"))}</dt><dd data-patrol-source="applied_at">${escape(result.applied_at)}</dd></dl>
      ${(result.changes || []).length ? `<h4>${escape(t("patrol.feedback.changes"))}</h4><ul>${result.changes.map(item => `<li>${escape(t("patrol.feedback.change", { operation: label(operations, item.operation || "supplement"), section: label(sections, item.section), quote: item.quote }))}</li>`).join("")}</ul>` : ""}
      <h4>${escape(t("patrol.feedback.evidence"))}</h4>${evidence.length ? evidence.map(item => `<pre>${escape(JSON.stringify(item, null, 2))}</pre>`).join("") : `<p>${escape(t("patrol.feedback.no_evidence"))}</p>`}
      ${related.length ? `<h4>${escape(t("patrol.feedback.cancellations"))}</h4><ul>${related.map(item => `<li data-patrol-cancellation-state="${escape(item.state)}" data-patrol-target-kind="${escape(item.target_kind)}" data-patrol-target-id="${escape(item.target_id)}"><strong>${escape(label(cancellations, item.state, t(cancellations.unknown)))}</strong><p>${escape(item.reason)}</p><small>${escape(label(targets, item.target_kind))} · ${escape(item.target_id)}</small></li>`).join("")}</ul>` : ""}</div></details>`;
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
  const pendingInput = row => !row.processing_state || ["pending", "observed"].includes(row.processing_state) || row.processing_result?.disposition === "deferred";
  const rounds = projection => ({ ...projection?.rounds, ...(projection?.round ? { [projection.round.entity_id]: projection.round } : {}) });
  const changed = (before, after) => before !== after && JSON.stringify(before) !== JSON.stringify(after);
  function cancellationTarget(projection, item) {
    const entity = projection?.[{ run: "runs", directive: "directives", worker: "workers" }[item.target_kind]]?.[item.target_id];
    const state = entity?.state;
    // Directive 的停止状态可能最终由它启动的 Run 结算。
    const run = item.target_kind === "directive" ? projection?.runs?.[state?.launched_run_id || state?.run_id]?.state : null;
    // Worker 没有独立 Live collection 时，其 Round 的屏障是现有结算通知。
    const round = item.target_kind === "worker" && !entity ? rounds(projection)[item.round_id]?.state : null;
    return [state?.status, state?.state, state?.lifecycle_state, state?.settled_at, state?.settlement_status, state?.completed_at,
      state?.launched_run_id, state?.run_id, run?.status, run?.settled_at, run?.settlement_status, round?.status, round?.barrier];
  }
  // observe 按 Live 差量累计脏输入；请求开始时 take，失败时 restore。
  // 请求期间的新变化留在下一批，客户端仍持有唯一在途请求；空队列的 take 用于手动刷新/重试首页。
  function createInputRefresh() {
    let previous = null, latest = false;
    let cachedRows = null, loaded = new Set(), pending = [], cancellations = [];
    const ids = new Set();
    const restore = plan => { latest ||= plan.latest; for (const id of plan.intentIds) ids.add(id); };
    return Object.freeze({
      observe(projection, model) {
        const before = previous;
        previous = projection;
        if (!before || before.loop_id !== projection.loop_id || before === projection) return false;
        if (model.rows !== cachedRows) {
          cachedRows = model.rows;
          loaded = new Set(model.rows.map(row => row.intent_id).filter(Boolean));
          pending = model.rows.filter(row => row.intent_id && pendingInput(row));
          cancellations = model.rows.flatMap(row => row.intent_id ? (row.processing_result?.related_cancellations || [])
            .filter(item => ["cancel_requested", "unknown"].includes(item.state)).map(item => ({ id: row.intent_id, item })) : []);
        }
        const affected = new Set();
        let readLatest = false;
        if (before.interventions !== projection.interventions) {
          for (const [id, entity] of Object.entries(projection.interventions || {})) {
            if (changed(before.interventions?.[id], entity)) {
              if (loaded.has(id)) affected.add(id);
              readLatest = true;
            }
          }
        }
        const committed = new Set(), oldRounds = rounds(before);
        for (const [id, entity] of Object.entries(rounds(projection))) {
          const state = entity.state || {};
          if (state.authority_committed_at && (state.authority_committed_at !== oldRounds[id]?.state?.authority_committed_at
            || state.decision_id !== oldRounds[id]?.state?.decision_id)) committed.add(id);
        }
        readLatest ||= committed.size > 0;
        // 延期输入可在后继 Round 或新 Loop 中再次采用；不能仅依赖缓存中的旧 round_id。
        if (committed.size) for (const row of pending) affected.add(row.intent_id);
        for (const { id, item } of cancellations) {
          if (changed(cancellationTarget(before, item), cancellationTarget(projection, item))) affected.add(id);
        }
        if (!readLatest && !affected.size) return false;
        restore({ latest: readLatest, intentIds: affected });
        return true;
      },
      take() {
        const plan = { latest: latest || !ids.size, intentIds: [...ids] };
        latest = false; ids.clear();
        return plan;
      },
      restore,
    });
  }
  async function refreshInputs(api, workspaceId, model, plan = { latest: true, intentIds: [] }) {
    const page = plan.latest ? await api.workspaceInputs(workspaceId) : { items: [], next_before: model.cursor };
    const latest = new Set(page.items.map(row => row.intent_id));
    const older = [...new Set(plan.intentIds)].filter(id => id && !latest.has(id));
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
    return pending ? { text: t("patrol.recovery.saved"), label: t("patrol.recovery.process") }
      : { text: status === "stopped" ? t("patrol.recovery.stopped") : t("patrol.recovery.failed"), label: t("panel.restart") };
  }
  // A committed verification identifies a delivery; waiting for input alone never does.
  function deliveryVerification(loop) {
    if (loop?.interaction_mode !== "workspace_patrol") return null;
    const verification = loop.final_result?.verification || loop.delivery;
    return verification?.verification_id && verification.conclusion === "satisfied" ? verification : null;
  }
  function isCurrentDelivery(loop) {
    const result = loop?.final_result;
    return Boolean(deliveryVerification(loop) && result?.round_id &&
      result.round_id === loop.current_round_id &&
      (result.goal_revision == null || result.goal_revision === loop.goal_revision));
  }
  function delivery(projection, workspace) {
    const loop = projection?.loop?.state, verification = deliveryVerification(loop);
    if (!verification || !loop.final_result) return "";
    const result = loop.final_result, criteria = verification.criteria || [];
    const checks = new Map((result.completion_checks || []).map(item => [item.check_id, item.claim]));
    const unresolved = new Set(verification.unresolved || []);
    const passed = criteria.filter(item => item.status === "satisfied" && !unresolved.has(item.check_id));
    const byId = new Map(criteria.map(item => [item.check_id, item]));
    const remaining = [...new Set([...criteria.filter(item => item.status !== "satisfied").map(item => item.check_id), ...unresolved])]
      .map(id => byId.get(id) || { check_id: id });
    const renderCheck = item => `<li><strong>${escape(checks.get(item.check_id) || item.check_id)}</strong>${item.explanation ? `<p>${escape(item.explanation)}</p>` : ""}</li>`;
    return `<header><h2>${escape(t("panel.delivery"))}</h2><span>${escape(t(isCurrentDelivery(loop) ? "patrol.delivery.verified" : "patrol.delivery.previous"))}</span></header>
      <p class="patrol-delivery-summary">${escape(result.summary || passed.map(item => item.explanation).join(t("patrol.feedback.separator")) || t("patrol.delivery.summary"))}</p>
      <nav aria-label="${escape(t("patrol.delivery.view"))}">${(result.final_path || []).map(item => `<button type="button" data-patrol-delivery-context="${escape(item.context_id)}"${item.revision_id ? ` data-revision-id="${escape(item.revision_id)}"` : ""} aria-label="${escape(t("patrol.delivery.view_result_action", { title: item.title || t("patrol.delivery.result") }))}">${escape(item.title || t("patrol.delivery.result"))}</button>`).join("")}<button type="button" data-patrol-files-open>${escape(t("patrol.delivery.workspace_files"))}</button></nav>
      ${remaining.length ? `<section class="patrol-delivery-unresolved" aria-label="${escape(t("patrol.delivery.unresolved"))}"><h3>${escape(t("patrol.delivery.remaining_summary", { count: remaining.length }))}</h3><ul>${remaining.map(renderCheck).join("")}</ul></section>` : ""}
      <details class="patrol-delivery-evidence"><summary>${escape(t("patrol.delivery.evidence_summary", { count: passed.length }))}</summary><ul>${passed.map(renderCheck).join("") || `<li>${escape(t("patrol.delivery.no_checks"))}</li>`}</ul>${workspace?.path ? `<h3>${escape(t("patrol.delivery.workspace"))}</h3><p>${escape(workspace.path)}</p>` : ""}</details>`;
  }
  // 工作、发送回执与状态更新各有独立语义；两个工作入口共享同一投影。
  function presentation(projection, connection, model) {
    const latest = model?.rows?.[0], loop = projection?.loop?.state;
    const saved = recovery(projection, model);
    const receipt = latest?.status === "accepted" && saved ? t("patrol.work.saved")
      : ({ accepted: t("patrol.work.receipt_accepted"), sending: t("patrol.work.receipt_sending"), failed: t("patrol.work.receipt_failed") }[latest?.status] || "");
    const stale = ["unavailable", "reconnecting", "resyncing"].includes(connection?.status);
    const sync = stale ? t("patrol.work.stale") : connection?.status !== "live" ? t("patrol.work.loading") : "";
    let work;
    if (latest?.status === "accepted" && (!loop || (latest.accepted_sequence != null && (projection.loop?.updated_sequence || 0) < latest.accepted_sequence))) {
      work = { state: "accepted", text: t("patrol.status.accepted") };
    } else if (loop?.status === "waiting_user" && loop.waiting_reason === "awaiting_input") {
      work = isCurrentDelivery(loop) ? { state: "delivered", text: t("patrol.work.delivered") }
        : { state: "idle", text: t("patrol.status.idle") };
    } else if (loop?.status === "completed" && isCurrentDelivery(loop)) {
      work = { state: "delivered", text: t("patrol.work.delivered") };
    } else {
      const labels = { running: t("patrol.work.running"), waiting_user: t("patrol.status.waiting_user"), paused: t("patrol.work.paused"), stopped: t("patrol.work.stopped"), failed: t("patrol.work.failed"), completed: t("patrol.work.unverified") };
      work = labels[loop?.status] ? { state: loop.status, text: labels[loop.status] }
        : { state: "unknown", text: t("patrol.work.loading") };
    }
    return { work, receipt, sync: !loop && stale ? t("patrol.work.unavailable") : sync };
  }
  function status(projection, connection, model) {
    if (["unavailable", "reconnecting", "resyncing"].includes(connection?.status)) return { state: "disconnected", text: t("patrol.status.disconnected") };
    const latest = model?.rows?.[0];
    if (latest?.status === "failed") return { state: "failed", text: t("patrol.status.send_failed") };
    if (latest?.status === "sending") return { state: "sending", text: t("patrol.status.sending") };
    const work = presentation(projection, connection, model).work;
    if (work.state !== "unknown") return work;
    return latest?.status === "accepted" ? { state: "accepted", text: t("patrol.status.accepted") } : { state: "idle", text: t("patrol.status.ready") };
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
      title: entity.state.title || entity.state.description || t("patrol.answer.committed") }));
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
    } catch (error) { signal?.throwIfAborted(); if (error.name === "AbortError") throw error; return { ...base, error: t("patrol.answer.read_failed") }; }
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
  return Object.freeze({ presentation, status, recovery, answer, signature, results, createReader, inputResult, preserveInputResults, createInputRefresh, refreshInputs, deliveryVerification, isCurrentDelivery, delivery });
});
