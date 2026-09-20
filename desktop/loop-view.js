/*
 * 本文件对外提供 Agent Loop 生命周期外壳、Option 3 Patrol 活动轨、Mission 表单、授权控制与轻量生命周期补丁函数。
 * 输入为兼容 Loop 读模型、权威 Live projection、连接状态、当前 Context 与 Console 读模型；输出为紧凑顶栏、结构化 Patrol 记录抽屉及现有图/会话/事实工作区。
 * 具体工作流为启动态编辑 Mission；运行态只呈现已提交 Patrol/curator 活动和最后一致 projection，并把图、会话、事实交给专用视图。
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
    const compressionEnabled = (grant.delegable_gates || []).includes("compression") && Boolean(grant.compression_policy?.version);
    return `<details class="loop-grant"><summary>Delegation 授权与预算</summary><form id="agentLoopGrantForm"><label class="loop-delegation-option"><input name="autonomousCompression" type="checkbox"${compressionEnabled ? " checked" : " disabled"}>允许 Patrol 自主压缩当前 Context（可恢复；取消勾选会立即撤销且不能在此处重新扩大）</label><label>Capabilities（逗号分隔，只能删除）<textarea name="capabilities" required>${list(grant.capabilities)}</textarea></label><label>Context scope（逗号分隔，只能删除）<textarea name="contextScope" required>${list(grant.context_scope)}</textarea></label><label>Permission scope（逗号分隔，只能删除）<textarea name="permissionScope">${list(grant.permission_scope)}</textarea></label><label>Delegable gates（逗号分隔，只能删除）<textarea name="delegableGates">${list(grant.delegable_gates)}</textarea></label><label>更早到期时间（ISO，可留空）<input name="expiresAt" value="${escape(grant.expires_at || "")}"></label><button type="submit">收窄授权并重新观察</button></form><form id="agentLoopBudgetForm"><div class="loop-start-budget">${budgetInputs(grant.budgets || {})}</div><button type="submit">更新预算并重新观察</button></form><button class="text-button danger" type="button" data-action="loop-revoke-grant">撤销 Patrol 授权</button></details>`;
  }

  function startView(context) {
    const defaults = { max_rounds: 50, max_duration_seconds: 86400, max_model_calls: 200, max_input_tokens: 2000000, max_output_tokens: 500000, max_retries: 20, max_lanes: 8, max_contexts: 16, max_providers: 4, max_new_lanes_per_round: 3, max_concurrent_runs: 4, max_no_progress: 3 };
    const initialRun = freshDirectRun(context);
    const linkedRun = context.active_run?.loop_id || context.latest_direct_user_run?.loop_id;
    const readiness = initialRun
      ? `<p class="loop-ready" role="status">首轮将绑定直接用户 Run：${escape(initialRun.run_id)}（${escape(initialRun.status || "unknown")}）</p>`
      : `<div class="loop-start-gate" role="status"><strong>${linkedRun ? "旧 Run 已归属于历史 Loop" : "还没有可用于首轮的直接用户 Run"}</strong><span>请回到当前 Context 发送一条新的用户消息，再创建后继 Loop。</span><button type="button" data-action="loop-new-run">返回 Context 发送消息</button></div>`;
    const editor = globalThis.FocusLoopMissionEditor?.render({ outcome: context.title || "" }) || "";
    return `<section class="loop-empty"><header><span class="loop-kicker">Context Loop</span><h2>启动长期 Agent Loop</h2><p>用户保留根权力；Portfolio Patrol 在授权范围内持续判断、策展 Context 并发送普通 HumanMessage。</p></header>${readiness}<form id="agentLoopStartForm" class="loop-start-form">${editor}<label class="loop-delegation-option"><input name="autonomousCompression" type="checkbox" checked>允许 Patrol 自主压缩 Context；原文保留可恢复，授权可随时撤销</label><label class="loop-delegation-option"><input name="isolatedWrites" type="checkbox" checked>允许 Patrol 为并行写实验创建隔离 Git worktree，并在明确选择后采用结果</label><div class="loop-start-budget">${budgetInputs(defaults)}</div><button class="primary" type="submit"${initialRun ? "" : " disabled"}>授权 Patrol 并启动</button></form></section>`;
  }

  function compressionStatus(related) {
    const candidate = (related.audit?.compression_candidates || []).at(-1);
    const resolution = (related.audit?.compression_resolutions || []).at(-1);
    const pending = (related.audit?.pending_decisions || []).filter(item => item.kind === "compression").at(-1);
    if (!pending && !candidate && !resolution) return "";
    const snapshot = related.snapshot || {};
    const status = resolution?.status || candidate?.status || (snapshot.health === "deciding" ? "preparing" : "threshold");
    const labels = { threshold: "已达到压缩阈值", preparing: "正在准备候选", prepared: "候选已准备", accepted: "已提交", committed: "Kernel 已提交", resuming: "正在恢复 Run", applied: "压缩已应用", superseded: "已被用户或新版本取代", failed: "压缩失败", expired: "候选已过期" };
    const actualReduction = resolution?.actual_reduction;
    const estimatedReduction = candidate?.estimated_reduction;
    const reduction = actualReduction ?? estimatedReduction;
    const reductionKind = actualReduction == null ? "预计" : "实际";
    const decision = (related.audit?.decisions || []).find(item => item.decision_id === resolution?.decision_id);
    const protectedEvidence = candidate?.protection_evidence || [];
    const ranges = candidate?.source_ranges || [];
    const contextId = candidate?.context_id || pending?.payload?.context_id;
    const detail = candidate || resolution ? `<details><summary>查看压缩证据与恢复入口</summary><dl><div><dt>目标 Context</dt><dd>${escape(contextId || "—")}</dd></div><div><dt>来源范围</dt><dd>${escape(ranges.map(item => `${(item.source_ids || []).length} 条消息`).join("，") || "—")}</dd></div><div><dt>保护锚点</dt><dd>${escape(protectedEvidence.map(item => item.reason).filter(Boolean).join("，") || "未覆盖受保护范围")}</dd></div><div><dt>预计 Token</dt><dd>${escape(candidate ? `${candidate.before_tokens} → ${candidate.after_tokens}` : "—")}</dd></div><div><dt>实际 Token</dt><dd>${escape(resolution?.actual_before_tokens == null || resolution?.actual_after_tokens == null ? "等待稳定 checkpoint 证据" : `${resolution.actual_before_tokens} → ${resolution.actual_after_tokens}`)}</dd></div><div><dt>Patrol 判断</dt><dd>${escape(decision?.rationale || "候选准备中")}</dd></div><div><dt>Kernel / Resume</dt><dd>${escape(resolution ? `${resolution.status}${resolution.run_id ? ` · Run ${resolution.run_id}` : ""}` : "尚未提交")}</dd></div><div><dt>结果 Revision</dt><dd>${escape(resolution?.result_context_revision_id || "—")}</dd></div></dl>${contextId ? `<button type="button" class="text-button" data-action="loop-select-context" data-context-id="${escape(contextId)}">打开完整 Context 会话并查看/恢复来源</button>` : ""}</details>` : "";
    return `<section class="loop-compression-status is-${escape(status)}" role="status" aria-live="polite"><strong>Patrol Context 压缩 · ${escape(labels[status] || status)}</strong><span>${escape(contextId || "当前 Context")}${Number.isFinite(Number(reduction)) ? ` · ${reductionKind}减少 ${escape(reduction)} tokens` : ""}</span>${candidate ? `<small>${escape(ranges.map(item => `${(item.source_ids || []).length} 条消息`).join("，"))}</small>` : ""}${detail}</section>`;
  }

  function goalDisclosure(loop) {
    const mission = globalThis.FocusLoopMissionEditor?.normalizeMission(loop.mission || { goal: loop.goal }) || { outcome: loop.goal?.goal || "Agent Loop", boundaries: {}, completion_checks: [] };
    const boundaryGroups = [["允许触及的范围", mission.boundaries.in_scope], ["必须保持", mission.boundaries.required_invariants], ["禁止 / 超范围", mission.boundaries.prohibited_actions]].filter(([, items]) => items?.length);
    const boundaries = boundaryGroups.map(([title, items]) => `<h4>${escape(title)}</h4><ul>${items.map(item => `<li>${escape(item)}</li>`).join("")}</ul>`).join("");
    const legacy = mission.boundaries.legacy_text ? `<h4>旧版边界原文</h4><p>${escape(mission.boundaries.legacy_text)}</p>` : "";
    return `<details class="loop-goal-disclosure"><summary title="${escape(mission.outcome)}">${escape(mission.outcome)} · Mission R${escape(loop.active_mission_revision || loop.goal_revision)}</summary><div class="loop-goal-detail"><h3>最终结果</h3><p>${escape(mission.outcome)}</p>${boundaries || legacy ? `<h3>执行边界</h3>${boundaries}${legacy}` : ""}${mission.completion_checks.length ? `<h3>完成检查</h3><ul>${mission.completion_checks.map(item => `<li><code>${escape(item.check_id)}</code> · ${escape(item.claim)} · ${escape(item.user_verification ? "用户确认" : (item.expected_evidence_kinds || []).join(" / "))}</li>`).join("")}</ul>` : ""}</div></details>`;
  }

  function missionHistory(history) {
    const revisions = history?.revisions || [];
    if (!revisions.length) return "";
    return `<details class="loop-mission-history"><summary>Mission 历史 · ${escape(revisions.length)} 个 revision</summary><div>${revisions.map(item => item.kind === "structured_mission" ? `<article class="loop-mission-history-item${item.active ? " is-active" : ""}"><strong>Mission R${escape(item.revision)}${item.active ? " · 当前" : ""}</strong><p>${escape(item.outcome)}</p></article>` : `<article class="loop-mission-history-item${item.active ? " is-active" : ""}"><strong>旧版 Goal Contract R${escape(item.revision)}${item.active ? " · 当前" : ""}</strong><p>${escape(item.goal)}</p><h4>旧版 Task Contract 原文</h4><pre>${escape(item.task_contract)}</pre></article>`).join("")}</div></details>`;
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

  function timeLabel(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function patrolActivity(state) {
    const live = state.live;
    const patrol = live?.patrol_session?.state;
    if (!live || !patrol) return "";
    const round = live.round?.state?.number || "—";
    const entries = (live.activity_timeline || []).filter(item => ["patrol_session", "curator", "directive"].includes(item.entity_type)).slice(-12).reverse();
    const curators = Object.values(live.curators || {}).sort((left, right) => right.updated_sequence - left.updated_sequence);
    const connection = state.connection || { status: "idle" };
    const connectionLabel = ({ live: "实时", syncing: "同步中", connecting: "连接中", reconnecting: "重连中", resyncing: "重同步", idle: "离线" })[connection.status] || connection.status;
    const rows = entries.map(item => `<li data-event-id="${escape(item.event_id)}"><time>${escape(timeLabel(item.occurred_at))}</time><span>${escape(item.summary)}</span><small>${escape(item.kind)}</small></li>`).join("");
    const workers = curators.map(item => `<li data-curator-id="${escape(item.entity_id)}"><span class="curator-state is-${escape(item.state.state)}">${escape(item.state.state)}</span><strong>${escape(item.state.scope || item.entity_id)}</strong><small>${escape(item.state.safe_summary || "等待结构化结果")}</small></li>`).join("");
    return `<section class="patrol-activity-rail" aria-label="Patrol 实时活动"><div class="patrol-activity-now" aria-live="polite" aria-atomic="true"><span class="loop-kicker">Round ${escape(round)} · Patrol</span><strong>${escape(patrol.safe_summary || patrol.summary || patrol.phase || patrol.status)}</strong><small>${escape(patrol.wait_reason || `阶段：${patrol.phase || "observing"}`)}</small></div><span class="live-connection is-${escape(connection.status)}"><i aria-hidden="true"></i>${escape(connectionLabel)}</span><details class="patrol-activity-drawer"><summary>查看记录</summary><div class="patrol-drawer-panel"><header><div><span class="loop-kicker">Patrol Activity</span><h3>Round ${escape(round)} · ${escape(patrol.phase || patrol.status)}</h3></div><span>${escape(patrol.status)}</span></header><section><h4>结构化活动</h4><ol>${rows || "<li><span>尚无已提交活动</span></li>"}</ol></section><section><h4>并行 Curators</h4><ul>${workers || "<li><span>本轮未分派 Curator</span></li>"}</ul></section></div></details></section>`;
  }

  function reconcileKeyedList(current, next, attribute) {
    if (!current || !next) return;
    const existing = new Map([...current.children].map(item => [item.getAttribute(attribute), item]));
    for (const item of [...next.children]) {
      const key = item.getAttribute(attribute);
      const prior = key ? existing.get(key) : null;
      if (!prior) current.append(item);
      else {
        existing.delete(key);
        if (prior.innerHTML !== item.innerHTML) prior.innerHTML = item.innerHTML;
        prior.className = item.className;
        current.append(prior);
      }
    }
    existing.forEach(item => item.remove());
  }

  function render(state, context = {}, consoleState = null) {
    const loop = state?.snapshot;
    if (!loop) return startView(context);
    const usage = loop.usage || {};
    const budgets = loop.grant?.budgets || {};
    const related = state.related || {};
    const activeRuns = (related.audit?.runs || []).filter(run => ["pending", "running"].includes(run.status));
    const terminal = TERMINAL.has(loop.status);
    const missionEditor = globalThis.FocusLoopMissionEditor?.render(loop.mission || { goal: loop.goal }) || "";
    const history = missionHistory(related.audit?.mission_history);
    const override = ["running", "paused", "waiting_user"].includes(loop.status) ? `<details class="loop-override"><summary>用户接管 / 修订 Mission</summary><form id="agentLoopOverrideForm">${missionEditor}<button class="primary" type="submit">确认新 Mission revision</button></form></details>` : "";
    const budgetDetails = `<div class="loop-budget"><span>Rounds ${escape(usage.rounds || 0)} / ${escape(budgets.max_rounds || "∞")}</span><span>Duration ${escape(usage.duration_seconds || 0)} / ${escape(budgets.max_duration_seconds || "∞")}s</span><span>Calls ${escape(usage.model_calls || 0)} / ${escape(budgets.max_model_calls || "∞")}</span><span>Input ${escape(usage.input_tokens || 0)} / ${escape(budgets.max_input_tokens || "∞")}</span><span>Output ${escape(usage.output_tokens || 0)} / ${escape(budgets.max_output_tokens || "∞")}</span><span>Retries ${escape(usage.retries || 0)} / ${escape(budgets.max_retries ?? "∞")}</span><span>Lanes ${escape(usage.lanes || 0)} / ${escape(budgets.max_lanes || "∞")}</span><span>Contexts ${escape(usage.contexts || 0)} / ${escape(budgets.max_contexts || "∞")}</span><span>Providers ${escape(usage.providers || 0)} / ${escape(budgets.max_providers || "∞")}</span></div>`;
    const consoleHtml = globalThis.FocusLoopConsoleView?.render({ ...consoleState, loopStatus: loop.status, terminal }) || '<section class="loop-console-loading">控制台模块不可用</section>';
    return `<section class="loop-dashboard console-shell" data-loop-id="${escape(loop.loop_id)}" data-loop-status="${escape(loop.status)}">${commandBar(loop, consoleState)}${patrolActivity(state)}${loop.waiting_reason ? `<p class="loop-waiting" data-loop-waiting role="status">${escape(loop.waiting_reason)}</p>` : ""}${compressionStatus({ ...related, snapshot: loop })}${terminal ? `<div class="loop-terminal-notice" role="status"><strong>该 Loop 已${loop.status === "completed" ? "完成" : loop.status === "failed" ? "失败" : "停止"}</strong><span>历史 Context、完整会话和事实证据仍可查看；退出不会删除审计记录。</span></div>` : ""}${consoleHtml}<details class="loop-advanced"><summary>授权、预算与目标控制</summary>${budgetDetails}${grantControls(loop)}${history}${override}<p class="muted tiny">当前轮次 ${escape(loop.current_round_id || "—")} · 活动 Run ${escape(activeRuns.length)} · Mission R${escape(loop.active_mission_revision || loop.goal_revision)} · Authority R${escape(loop.authority_revision)}</p></details></section>`;
  }

  function patchLifecycle(container, state) {
    const loop = state?.snapshot;
    const shell = container?.querySelector?.(".loop-dashboard.console-shell");
    if (!loop || !shell || shell.dataset.loopId !== loop.loop_id) return false;
    if (shell.dataset.loopStatus !== loop.status) return false;
    const waiting = shell.querySelector("[data-loop-waiting]");
    if (Boolean(waiting) !== Boolean(loop.waiting_reason)) return false;
    if (waiting) waiting.textContent = loop.waiting_reason;
    const commandTemplate = document.createElement("template");
    commandTemplate.innerHTML = commandBar(loop, null);
    const metrics = shell.querySelector(".loop-command-metrics");
    const nextMetrics = commandTemplate.content.querySelector(".loop-command-metrics");
    if (metrics && nextMetrics) metrics.replaceWith(nextMetrics);
    const activity = shell.querySelector(".patrol-activity-rail");
    const nextActivityHtml = patrolActivity(state);
    if (activity && nextActivityHtml) {
      const template = document.createElement("template");
      template.innerHTML = nextActivityHtml;
      const replacement = template.content.firstElementChild;
      activity.querySelector(".patrol-activity-now")?.replaceWith(replacement.querySelector(".patrol-activity-now"));
      activity.querySelector(".live-connection")?.replaceWith(replacement.querySelector(".live-connection"));
      const drawer = activity.querySelector(".patrol-activity-drawer");
      const nextDrawer = replacement.querySelector(".patrol-activity-drawer");
      drawer?.querySelector("header")?.replaceWith(nextDrawer?.querySelector("header"));
      reconcileKeyedList(drawer?.querySelector("ol"), nextDrawer?.querySelector("ol"), "data-event-id");
      reconcileKeyedList(drawer?.querySelector("ul"), nextDrawer?.querySelector("ul"), "data-curator-id");
    } else if (Boolean(activity) !== Boolean(nextActivityHtml)) {
      return false;
    }
    return true;
  }

  return Object.freeze({ render, patchLifecycle });
});
