/*
 * 本文件对外提供 Agent Loop 生命周期外壳、Portfolio Map 紧凑顶栏、启动表单、授权控制与轻量生命周期补丁函数。
 * 输入为 Loop Store、当前 Context 与独立 Console Store；输出为可创建 Loop 的启动页，或承载真实 Context 图、完整会话和事实表的运行/历史壳层。
 * 具体工作流为筛选尚未绑定 Loop 的直接用户 Run，按生命周期呈现控制与终止态出口，并把拓扑、会话、事实和介入委托给 Console View。
 * 示例：`FocusLoopView.render(loopState, context, consoleState)`；SSE 到达时调用 `patchLifecycle(container, state)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const TERMINAL = new Set(["completed", "stopped", "failed"]);
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

  function freshDirectRun(context) {
    return [context.active_run, context.latest_direct_user_run].find(run => run?.run_id && !run.loop_id && (!run.origin || run.origin === "direct_user")) || null;
  }

  function budgetPercent(usage, budgets) {
    const dimensions = [
      [usage.rounds, budgets.max_rounds],
      [usage.model_calls, budgets.max_model_calls],
      [usage.input_tokens, budgets.max_input_tokens],
      [usage.output_tokens, budgets.max_output_tokens],
    ].filter(([, maximum]) => Number(maximum) > 0);
    return Math.min(100, Math.max(0, ...dimensions.map(([used, maximum]) => Math.round((Number(used || 0) / Number(maximum)) * 100))));
  }

  function grantControls(loop) {
    if (!loop.grant || !["running", "paused", "waiting_user"].includes(loop.status)) return "";
    const grant = loop.grant;
    return `<details class="loop-grant"><summary>Delegation 授权与预算</summary><form id="agentLoopGrantForm"><label>Capabilities（逗号分隔，只能删除）<textarea name="capabilities" required>${list(grant.capabilities)}</textarea></label><label>Context scope（逗号分隔，只能删除）<textarea name="contextScope" required>${list(grant.context_scope)}</textarea></label><label>Permission scope（逗号分隔，只能删除）<textarea name="permissionScope">${list(grant.permission_scope)}</textarea></label><label>Delegable gates（逗号分隔，只能删除）<textarea name="delegableGates">${list(grant.delegable_gates)}</textarea></label><label>更早到期时间（ISO，可留空）<input name="expiresAt" value="${escape(grant.expires_at || "")}"></label><button type="submit">收窄授权并重新观察</button></form><form id="agentLoopBudgetForm"><div class="loop-start-budget">${budgetInputs(grant.budgets || {})}</div><button type="submit">更新预算并重新观察</button></form><button class="text-button danger" type="button" data-action="loop-revoke-grant">撤销 Patrol 授权</button></details>`;
  }

  function startView(context) {
    const defaults = { max_rounds: 50, max_duration_seconds: 86400, max_model_calls: 200, max_input_tokens: 2000000, max_output_tokens: 500000, max_retries: 20, max_lanes: 8, max_contexts: 16, max_providers: 4, max_new_lanes_per_round: 3, max_concurrent_runs: 4, max_no_progress: 3 };
    const initialRun = freshDirectRun(context);
    const linkedRun = context.active_run?.loop_id || context.latest_direct_user_run?.loop_id;
    const readiness = initialRun
      ? `<p class="loop-ready" role="status">首轮将绑定直接用户 Run：${escape(initialRun.run_id)}（${escape(initialRun.status || "unknown")}）</p>`
      : `<div class="loop-start-gate" role="status"><strong>${linkedRun ? "旧 Run 已归属于历史 Loop" : "还没有可用于首轮的直接用户 Run"}</strong><span>请回到当前 Context 发送一条新的用户消息，再创建后继 Loop。</span><button type="button" data-action="loop-new-run">返回 Context 发送消息</button></div>`;
    return `<section class="loop-empty"><header><span class="loop-kicker">Context Loop</span><h2>启动长期 Agent Loop</h2><p>用户保留根权力；Portfolio Patrol 在授权范围内持续判断、策展 Context 并发送普通 HumanMessage。</p></header>${readiness}<form id="agentLoopStartForm" class="loop-start-form"><label>长期目标<textarea name="goal" required rows="3" placeholder="描述最终要达成的结果">${escape(context.title || "")}</textarea></label><label>Task Contract<textarea name="taskContract" required rows="5" placeholder="范围、约束、交付物与禁止事项"></textarea></label><label>验收条件（每行一项）<textarea name="criteria" required rows="5" placeholder="核心功能完成并通过测试&#10;没有未解决的高风险问题"></textarea></label><label class="loop-delegation-option"><input name="isolatedWrites" type="checkbox" checked>允许 Patrol 为并行写实验创建隔离 Git worktree，并在明确选择后采用结果</label><div class="loop-start-budget">${budgetInputs(defaults)}</div><button class="primary" type="submit"${initialRun ? "" : " disabled"}>授权 Patrol 并启动</button></form></section>`;
  }

  function goalDisclosure(loop) {
    const goal = loop.goal?.goal || "Agent Loop";
    const criteria = loop.goal?.acceptance_criteria || [];
    return `<details class="loop-goal-disclosure"><summary title="${escape(goal)}">${escape(goal)}</summary><div class="loop-goal-detail"><h3>长期目标</h3><p>${escape(goal)}</p>${loop.goal?.task_contract ? `<h3>Task Contract</h3><p>${escape(loop.goal.task_contract)}</p>` : ""}${criteria.length ? `<h3>验收条件</h3><ul>${criteria.map(item => `<li>${escape(item.text || item.criterion_id)}</li>`).join("")}</ul>` : ""}</div></details>`;
  }

  function commandBar(loop, consoleState) {
    const usage = loop.usage || {};
    const budgets = loop.grant?.budgets || {};
    const contextCount = consoleState?.manifest?.nodes?.length ?? usage.contexts ?? 0;
    const terminal = TERMINAL.has(loop.status);
    const controls = loop.status === "running" ? ["pause", "stop"] : loop.status === "paused" || loop.status === "waiting_user" ? [...(loop.grant ? ["resume"] : []), "stop"] : [];
    const percent = budgetPercent(usage, budgets);
    return `<header class="loop-command-bar"><div class="loop-command-title"><h2>Portfolio Map</h2>${goalDisclosure(loop)}</div><div class="loop-command-metrics"><div class="loop-command-metric"><span class="loop-live-dot is-${escape(loop.status)}" aria-hidden="true"></span><div><strong>${escape(loop.status)}</strong><small>Loop 状态</small></div></div><div class="loop-command-metric"><strong>Round ${escape(usage.rounds || 0)}</strong><small>/ ${escape(budgets.max_rounds || "∞")}</small></div><div class="loop-command-metric"><strong>${escape(contextCount)} 个 Context</strong><small>真实 Portfolio</small></div><div class="loop-command-metric"><strong>Portfolio Patrol</strong><small>${escape(loop.health || "idle")}</small></div><div class="loop-budget-ring" style="--loop-budget-progress:${percent}%" role="img" aria-label="预算已使用 ${percent}%"><span>${percent}%</span></div></div><div class="loop-primary-controls">${controls.map(command => `<button type="button" data-action="loop-control" data-loop-control="${command}">${({ pause: "暂停", resume: "恢复", stop: "停止" })[command]}</button>`).join("")}${terminal ? '<button type="button" data-action="loop-exit">退出当前 Loop</button><button class="primary" type="button" data-action="loop-prepare-new">新建 Loop</button>' : ""}</div></header>`;
  }

  function render(state, context = {}, consoleState = null) {
    const loop = state?.snapshot;
    if (!loop) return startView(context);
    const usage = loop.usage || {};
    const budgets = loop.grant?.budgets || {};
    const related = state.related || {};
    const activeRuns = (related.audit?.runs || []).filter(run => ["pending", "running"].includes(run.status));
    const terminal = TERMINAL.has(loop.status);
    const override = ["running", "paused", "waiting_user"].includes(loop.status) ? `<details class="loop-override"><summary>用户接管 / 改变方向</summary><form id="agentLoopOverrideForm"><label>新目标<textarea name="goal" required>${escape(loop.goal?.goal || "")}</textarea></label><label>新 Task Contract<textarea name="taskContract" required>${escape(loop.goal?.task_contract || "")}</textarea></label><label>验收条件（每行一项）<textarea name="criteria" required>${escape((loop.goal?.acceptance_criteria || []).map(item => item.text || item.criterion_id).join("\n"))}</textarea></label><button class="primary" type="submit">覆盖 Patrol 当前方向</button></form></details>` : "";
    const budgetDetails = `<div class="loop-budget"><span>Rounds ${escape(usage.rounds || 0)} / ${escape(budgets.max_rounds || "∞")}</span><span>Duration ${escape(usage.duration_seconds || 0)} / ${escape(budgets.max_duration_seconds || "∞")}s</span><span>Calls ${escape(usage.model_calls || 0)} / ${escape(budgets.max_model_calls || "∞")}</span><span>Input ${escape(usage.input_tokens || 0)} / ${escape(budgets.max_input_tokens || "∞")}</span><span>Output ${escape(usage.output_tokens || 0)} / ${escape(budgets.max_output_tokens || "∞")}</span><span>Retries ${escape(usage.retries || 0)} / ${escape(budgets.max_retries ?? "∞")}</span><span>Lanes ${escape(usage.lanes || 0)} / ${escape(budgets.max_lanes || "∞")}</span><span>Contexts ${escape(usage.contexts || 0)} / ${escape(budgets.max_contexts || "∞")}</span><span>Providers ${escape(usage.providers || 0)} / ${escape(budgets.max_providers || "∞")}</span></div>`;
    const consoleHtml = globalThis.FocusLoopConsoleView?.render({ ...consoleState, loopStatus: loop.status, terminal }) || '<section class="loop-console-loading">控制台模块不可用</section>';
    return `<section class="loop-dashboard console-shell" data-loop-id="${escape(loop.loop_id)}" data-loop-status="${escape(loop.status)}">${commandBar(loop, consoleState)}${loop.waiting_reason ? `<p class="loop-waiting" data-loop-waiting role="status">${escape(loop.waiting_reason)}</p>` : ""}${terminal ? `<div class="loop-terminal-notice" role="status"><strong>该 Loop 已${loop.status === "completed" ? "完成" : loop.status === "failed" ? "失败" : "停止"}</strong><span>历史 Context、完整会话和事实证据仍可查看；退出不会删除审计记录。</span></div>` : ""}${consoleHtml}<details class="loop-advanced"><summary>授权、预算与目标控制</summary>${budgetDetails}${grantControls(loop)}${override}<p class="muted tiny">当前轮次 ${escape(loop.current_round_id || "—")} · 活动 Run ${escape(activeRuns.length)} · Goal R${escape(loop.goal_revision)} · Authority R${escape(loop.authority_revision)}</p></details></section>`;
  }

  function patchLifecycle(container, state) {
    const loop = state?.snapshot;
    const shell = container?.querySelector?.(".loop-dashboard.console-shell");
    if (!loop || !shell || shell.dataset.loopId !== loop.loop_id) return false;
    if (shell.dataset.loopStatus !== loop.status) return false;
    const waiting = shell.querySelector("[data-loop-waiting]");
    if (Boolean(waiting) !== Boolean(loop.waiting_reason)) return false;
    if (waiting) waiting.textContent = loop.waiting_reason;
    return true;
  }

  return Object.freeze({ render, patchLifecycle });
});
