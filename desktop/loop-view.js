/*
 * 本文件对外提供 Agent Loop 生命周期、轮次、Patrol 阶段、预算和控制视图。
 * 输入为 Loop Store 的当前状态、当前 Context 与关联 Portfolio/Graph/audit/slot 投影；输出为启动表单
 * 或完整控制台 HTML。具体工作流为渲染状态、组合专用只读视图并把用户动作回传应用层；示例：
 * `FocusLoopView.render(state, context)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const list = values => escape((values || []).join(", "));
  function budgetInputs(budgets = {}) {
    const field = (name, label, value, min = 0, max = "") => `<label>${label}<input name="${name}" type="number" min="${min}"${max ? ` max="${max}"` : ""} value="${escape(budgets[value] ?? "")}" required></label>`;
    return [
      field("maxRounds", "最大轮次", "max_rounds", 1, 1000),
      field("maxDurationSeconds", "最长秒数", "max_duration_seconds", 60),
      field("maxModelCalls", "最大模型调用", "max_model_calls", 1),
      field("maxInputTokens", "最大输入 Token", "max_input_tokens", 1),
      field("maxOutputTokens", "最大输出 Token", "max_output_tokens", 1),
      field("maxRetries", "最大重试", "max_retries", 0),
      field("maxLanes", "最大 Lane", "max_lanes", 1, 64),
      field("maxContexts", "最大 Context", "max_contexts", 1, 256),
      field("maxProviders", "最大 Provider", "max_providers", 1, 32),
      field("maxNewLanesPerRound", "每轮最大新 Lane", "max_new_lanes_per_round", 0, 16),
      field("maxConcurrentRuns", "最大并行 Run", "max_concurrent_runs", 1, 32),
      field("maxNoProgress", "最大无进展轮次", "max_no_progress", 1, 20),
    ].join("");
  }

  function grantControls(loop) {
    if (!loop.grant || !["running", "paused", "waiting_user"].includes(loop.status)) return "";
    const grant = loop.grant;
    return `<details class="loop-grant"><summary>Delegation 授权与预算</summary><form id="agentLoopGrantForm"><label>Capabilities（逗号分隔，只能删除）<textarea name="capabilities" required>${list(grant.capabilities)}</textarea></label><label>Context scope（逗号分隔，只能删除）<textarea name="contextScope" required>${list(grant.context_scope)}</textarea></label><label>Permission scope（逗号分隔，只能删除）<textarea name="permissionScope">${list(grant.permission_scope)}</textarea></label><label>Delegable gates（逗号分隔，只能删除）<textarea name="delegableGates">${list(grant.delegable_gates)}</textarea></label><label>更早到期时间（ISO，可留空）<input name="expiresAt" value="${escape(grant.expires_at || "")}"></label><button type="submit">收窄授权并重新观察</button></form><form id="agentLoopBudgetForm"><div class="loop-start-budget">${budgetInputs(grant.budgets || {})}</div><button type="submit">更新预算并重新观察</button></form><button class="text-button danger" type="button" data-action="loop-revoke-grant">撤销 Patrol 授权</button></details>`;
  }

  function render(state, context = {}) {
    const loop = state?.snapshot;
    if (!loop) {
      const defaults = { max_rounds: 50, max_duration_seconds: 86400, max_model_calls: 200, max_input_tokens: 2000000, max_output_tokens: 500000, max_retries: 20, max_lanes: 8, max_contexts: 16, max_providers: 4, max_new_lanes_per_round: 3, max_concurrent_runs: 4, max_no_progress: 3 };
      const initialRun = context.active_run || context.latest_direct_user_run;
      const readiness = initialRun?.run_id ? `<p class="loop-ready" role="status">首轮将绑定直接用户 Run：${escape(initialRun.run_id)}（${escape(initialRun.status || "unknown")}）</p>` : '<p class="loop-waiting" role="status">请先在当前 Context 发送初始任务；该直接用户 Run 将成为 Loop 第一轮。</p>';
      return `<section class="loop-empty"><header><span class="loop-kicker">Context Loop</span><h2>启动长期 Agent Loop</h2></header><p>用户保留根权力；一个 Portfolio Patrol 在授权范围内持续判断、策展 Context 并发送普通 HumanMessage。</p>${readiness}<form id="agentLoopStartForm" class="loop-start-form"><label>长期目标<textarea name="goal" required rows="3" placeholder="描述最终要达成的结果">${escape(context.title || "")}</textarea></label><label>Task Contract<textarea name="taskContract" required rows="5" placeholder="范围、约束、交付物与禁止事项"></textarea></label><label>验收条件（每行一项）<textarea name="criteria" required rows="5" placeholder="核心功能完成并通过测试&#10;没有未解决的高风险问题"></textarea></label><label class="loop-delegation-option"><input name="isolatedWrites" type="checkbox" checked>允许 Patrol 为并行写实验创建隔离 Git worktree，并在明确选择后采用结果</label><div class="loop-start-budget">${budgetInputs(defaults)}</div><button class="primary" type="submit"${initialRun?.run_id ? "" : " disabled"}>授权 Patrol 并启动</button></form></section>`;
    }
    const usage = loop.usage || {};
    const budgets = loop.grant?.budgets || {};
    const controls = loop.status === "running" ? ["pause", "stop"] : loop.status === "paused" || loop.status === "waiting_user" ? [...(loop.grant ? ["resume"] : []), "stop"] : [];
    const related = state.related || {};
    const activeRuns = (related.audit?.runs || []).filter(run => ["pending", "running"].includes(run.status));
    const override = ["running", "paused", "waiting_user"].includes(loop.status) ? `<details class="loop-override"><summary>用户接管 / 改变方向</summary><form id="agentLoopOverrideForm"><label>新目标<textarea name="goal" required>${escape(loop.goal?.goal || "")}</textarea></label><label>新 Task Contract<textarea name="taskContract" required>${escape(loop.goal?.task_contract || "")}</textarea></label><label>验收条件（每行一项）<textarea name="criteria" required>${escape((loop.goal?.acceptance_criteria || []).map(item => item.text || item.criterion_id).join("\n"))}</textarea></label><button class="primary" type="submit">覆盖 Patrol 当前方向</button></form></details>` : "";
    const budgetView = `<section class="loop-budget"><h3>预算</h3><span>Rounds ${escape(usage.rounds || 0)} / ${escape(budgets.max_rounds || "∞")}</span><span>Duration ${escape(usage.duration_seconds || 0)} / ${escape(budgets.max_duration_seconds || "∞")}s</span><span>Calls ${escape(usage.model_calls || 0)} / ${escape(budgets.max_model_calls || "∞")}</span><span>Input ${escape(usage.input_tokens || 0)} / ${escape(budgets.max_input_tokens || "∞")}</span><span>Output ${escape(usage.output_tokens || 0)} / ${escape(budgets.max_output_tokens || "∞")}</span><span>Retries ${escape(usage.retries || 0)} / ${escape(budgets.max_retries ?? "∞")}</span><span>Lanes ${escape(usage.lanes || 0)} / ${escape(budgets.max_lanes || "∞")}</span><span>Contexts ${escape(usage.contexts || 0)} / ${escape(budgets.max_contexts || "∞")}</span><span>Providers ${escape(usage.providers || 0)} / ${escape(budgets.max_providers || "∞")}</span>`;
    return `<section class="loop-dashboard" data-loop-id="${escape(loop.loop_id)}"><header><div><span class="loop-kicker">Context Loop</span><h2>${escape(loop.goal?.goal || "Agent Loop")}</h2></div><span class="loop-status is-${escape(loop.status)}">${escape(loop.status)}</span></header><dl class="loop-facts"><div><dt>当前轮次</dt><dd>${escape(loop.current_round_id || "—")}</dd></div><div><dt>Patrol 阶段</dt><dd>${escape(loop.health)}</dd></div><div><dt>授权持有者</dt><dd>${escape(loop.holder_id)}</dd></div><div><dt>活动 Run</dt><dd>${escape(activeRuns.length)}</dd></div><div><dt>版本</dt><dd>Goal ${escape(loop.goal_revision)} · Authority ${escape(loop.authority_revision)}</dd></div></dl>${loop.waiting_reason ? `<p class="loop-waiting" role="status">${escape(loop.waiting_reason)}</p>` : ""}${budgetView}</section><div class="loop-controls">${controls.map(command => `<button type="button" data-action="loop-control" data-loop-control="${command}">${({ pause: "暂停", resume: "恢复", stop: "停止" })[command]}</button>`).join("")}</div>${grantControls(loop)}${override}${renderRelated(related, loop)}</section>`;
  }

  function renderRelated(related, loop) {
    const portfolio = portfolioSnapshot(related.portfolio, related.audit, related.slots);
    const portfolioHtml = globalThis.FocusPortfolioView?.render(portfolio.current, portfolio.previous) || "";
    const evolutionHtml = related.evolution ? globalThis.FocusContextEvolutionView?.render(related.evolution, related.tree, loop?.final_result) || "" : "";
    const slotsHtml = globalThis.FocusWorkspaceSlotsView?.render(related.slots || []) || "";
    const directives = related.audit?.directives || [];
    const auditHtml = `<section class="loop-audit"><h3>Delegated HumanMessages</h3>${directives.map(item => `<article><header>${globalThis.FocusMessageProvenanceView?.badge({ provenance_id: item.directive_id, source_kind: "delegated_patrol" }) || ""}<span>${escape(item.status)}</span></header><p>${escape(item.content)}</p><small>${escape(item.round_id || "")} · ${escape(item.directive_id)}</small></article>`).join("") || "<p>暂无委托消息</p>"}</section>`;
    return `<div class="loop-related">${portfolioHtml}${evolutionHtml}${slotsHtml}${auditHtml}</div>`;
  }

  function portfolioSnapshot(payload, audit = {}, slots = []) {
    if (!payload) return { current: {}, previous: null };
    const revisions = payload.portfolios || [];
    const convert = revision => ({ generation: revision?.generation || 0, lanes: (revision?.candidates || []).map(candidate => { const lane = (payload.lanes || []).find(item => item.lane_id === candidate.lane_id) || {}; const run = [...(audit.runs || [])].reverse().find(item => item.context_id === candidate.target_context_id); const slot = (slots || []).find(item => item.owner_lane_id === candidate.lane_id); return { lane_id: candidate.lane_id, purpose: lane.purpose || "Lane", action: candidate.action, revision_id: candidate.candidate_context_revision_id, freshness: candidate.status, source_frontier: candidate.source_allocation || revision.source_frontier || [], run_status: run?.status, adoption_state: slot?.adoption?.status || slot?.runs?.at?.(0)?.adoption_state }; }) });
    return { current: convert(revisions.at(-1)), previous: revisions.length > 1 ? convert(revisions.at(-2)) : null };
  }

  return Object.freeze({ render });
});
