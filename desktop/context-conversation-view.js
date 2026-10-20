/*
 * 本文件对外提供 Loop 中单个 Context 的固定 revision 会话、当前执行活动、真实 directive 因果带与三类介入面板。
 * 输入为分页会话、选中节点、canonical causality、来源审计、筛选与 Loop 生命周期；输出为状态摘要、Patrol/用户来源分明的因果事件、完整消息和运行期命令表单。
 * 具体工作流为当前执行活动读取 Live projection，固定 revision 历史会话维持有界双向分页；因果带只读取已提交事件，来源徽标保持在审计栏，正文不被元数据污染，终止态只读；
 * 标题与描述同源时只显示一次，不在页头重复渲染同一段 purpose。
 * 示例：`FocusContextConversationView.render(consoleState)`。
 * 直接消息分别展示受理、待授权、交付、运行、终态，用户来源 Directive 使用独立标签。
  * renderInspection 复用同一消息渲染的只读检查面，不附加投递方式表单或来源恢复写按钮。正文与任务/机器人共用安全 Markdown，工具与原始审计默认折叠。
 */
(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./message-reading.js") : root.FocusMessageReading);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextConversationView = api;
})(typeof globalThis === "object" ? globalThis : this, function (reading) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const role = message => String(message?.role || message?.type || "message").toLowerCase();
  const content = message => reading.text(message?.content);

  function visibleMessages(state) {
    const filter = state.messageFilter || "all";
    const search = String(state.messageSearch || "").trim().toLowerCase();
    return (state.conversation?.messages || []).filter(item => {
      const itemRole = role(item.message);
      const matchesRole = filter === "all" || itemRole === filter || (filter === "assistant" && itemRole === "ai");
      const matchesSearch = !search || content(item.message).toLowerCase().includes(search);
      return matchesRole && matchesSearch;
    });
  }

  function messageCard(item, readOnly = false, revision = null) {
    const message = item.message || {};
    const itemRole = role(message);
    const provenance = item.provenance;
    const compression = message.compression || message.additional_kwargs?.compression;
    const compressed = compression ? localize`<details class="compression-audit"><summary>${compression.deleted ? localize("已删除块") : localize("压缩块")} · 原始来源可恢复</summary><pre>${escape(JSON.stringify(compression.source || [], null, 2))}</pre>${message.id && !readOnly ? localize`<button type="button" class="text-button" data-action="loop-restore-compression" data-context-id="${escape(item.context_id || "")}" data-message-id="${escape(message.id)}">恢复原始消息并发布新 Revision</button>` : ""}</details>` : "";
    const fileContext = message.file_reference || message.file_context || { workspace_id: message.workspace_id, run_id: message.run_id, slot_id: message.slot_id };
    return `<article class="loop-message is-${escape(itemRole)}" data-message-index="${escape(item.index)}" data-message-role="${escape(itemRole)}" data-file-task-id="${escape(message.task_id || item.context_id || "")}" data-file-context="${escape(JSON.stringify(fileContext))}"><header><strong>${escape(itemRole)}</strong><span>#${item.index + 1}</span></header><div class="loop-message-content">${reading.render(message, { details: true, provenance: provenance || (["human", "user"].includes(itemRole) ? { source_kind: "direct_human" } : null), revision })}${compressed}</div></article>`;
  }

  function contextMetrics(node) {
    const counts = node.counts || {};
    return [
      [counts.runs, "Runs"],
      [counts.delegated_messages, localize("Patrol 指令")],
      [counts.workspace_changes, localize("工作区变更")],
      [counts.artifacts, localize("产物")],
    ].map(([value, label]) => `<div><strong>${escape(value || 0)}</strong><span>${label}</span></div>`).join("");
  }

  function causality(entries) {
    if (!entries?.length) return "";
    const visible = entries.slice(-8);
    return localize`<section class="context-causality" aria-label="指令因果链"><span class="loop-kicker">Live causality</span><ol>${visible.map(item => `<li data-causality-id="${escape(item.event_id)}" class="is-${escape(item.entity_type)}"><i aria-hidden="true"></i><strong>${escape(item.entity_type === "directive" ? (item.detail?.origin === "direct_user" ? localize("用户消息交付") : "Patrol directive") : item.entity_type)}</strong><span>${escape(item.summary)}</span><small>${escape(item.kind)}</small></li>`).join("")}</ol></section>`;
  }

  function userMessages(manifest, contextId) {
    const labels = { accepted: localize("已受理 · 待观察授权"), observed: localize("已观察 · 待 Patrol 授权"), delivered: localize("已交付 · 等待启动"), run_started: localize("运行中"), settled: localize("已结算"), failed: localize("失败"), delivery_failed: localize("交付失败"), rejected: localize("已拒绝"), cancelled: localize("已取消") };
    const messages = (manifest.user_intents || []).filter(item => item.intent_kind === "direct_message" && item.context_id === contextId).slice(0, 8);
    return messages.length ? localize`<section aria-label="用户消息交付状态" aria-live="polite">${messages.map(item => `<p data-user-intent-id="${escape(item.intent_id)}" data-delivery-state="${escape(item.delivery_state)}"><strong>${escape(labels[item.delivery_state] || item.delivery_state)}</strong>${item.resulting_run_id ? ` · Run ${escape(item.resulting_run_id)}` : ""}</p>`).join("")}</section>` : "";
  }

  function executionActivity(node) {
    const run = node.active_run;
    if (!run) return localize('<section class="context-execution-activity" aria-label="当前执行活动"><span class="loop-kicker">当前执行活动</span><strong>当前没有运行中的 Context Run</strong></section>');
    const action = node.live_action?.detail?.run_id === run.id ? node.live_action : null;
    const summary = run.last_activity_summary || action?.summary || (run.status === "pending" ? localize("Run 已交付，等待执行") : localize("Run 正在执行，等待首个已确认活动"));
    const occurredAt = run.last_activity_at || action?.occurred_at;
    const time = occurredAt ? new Date(occurredAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : localize("尚无已确认活动");
    return localize`<section class="context-execution-activity" aria-label="当前执行活动" aria-live="polite"><span class="loop-kicker">当前执行活动 · ${escape(run.status)}</span><strong>${escape(summary)}</strong><small>${escape(time)} · Run ${escape(run.id)}</small></section>`;
  }

  function render(state) {
    const manifest = state.manifest;
    const node = manifest?.nodes?.find(item => item.context_id === state.selectedContextId);
    const conversation = state.conversation;
    if (!node) return localize('<section class="context-conversation is-empty">选择一个 Context 查看完整会话</section>');
    const modes = [
      ["direct_context_message", localize("直接进入 Context"), localize("受理用户原文，观察并授权后交付该 Context；沿用其当前装备")],
      ["patrol_context_intent", localize("告诉 Patrol：这个 Context"), localize("意见进入 Patrol 下一次观察，不污染模型消息")],
      ["patrol_portfolio_intent", localize("告诉 Patrol：整体布局"), localize("对所有 Context 的分工、保留或淘汰提出意见")],
    ];
    const mode = modes.find(item => item[0] === state.interventionMode) || modes[0];
    const messages = visibleMessages(state);
    const name = node.topic || node.title;
    const purpose = node.purpose && node.purpose !== name ? `${escape(node.purpose)} · ` : "";
    const filters = ["all", "human", "assistant", "tool"];
    const historyControl = conversation?.has_more
      ? localize`<div class="history-sentinel" data-loop-history-sentinel aria-hidden="true"></div><button type="button" class="load-history" data-action="loop-load-older">加载更早消息（还有 ${conversation.range.start} 条）</button>`
      : localize('<p class="history-boundary">已到达该 revision 的会话起点</p>');
    const newerControl = conversation?.has_newer
      ? localize`<button type="button" class="load-history" data-action="loop-load-newer">加载更新消息（还有 ${conversation.total - conversation.range.end} 条）</button><div class="history-sentinel" data-loop-newer-sentinel aria-hidden="true"></div>`
      : localize('<p class="history-boundary">已到达该 revision 的最新消息</p>');
    const composer = state.terminal
      ? localize('<div class="loop-intervention is-readonly"><strong>历史只读</strong><span>该 Loop 已结束，退出后可在当前 Context 发送新的用户消息并创建后继 Loop。</span></div>')
      : `<form id="loopInterventionForm" class="loop-intervention"><div class="intervention-modes" role="tablist">${modes.map(item => `<button type="button" role="tab" data-action="loop-intervention-mode" data-mode="${item[0]}" aria-selected="${state.interventionMode === item[0]}" class="${state.interventionMode === item[0] ? "is-active" : ""}">${item[1]}</button>`).join("")}</div><p>${escape(mode[2])}</p><div class="intervention-composer"><textarea name="content" required rows="3" placeholder="${escape(mode[0] === "direct_context_message" ? localize("向这个 Context 发送新的 HumanMessage") : localize("表达你的意图，Patrol 将在下次判断中处理"))}"></textarea><button class="primary" type="submit" ${state.pending === "intervention" ? "disabled" : ""}>${state.pending === "intervention" ? localize("提交中…") : localize("发送")}</button></div></form>`;
    return localize`<section class="context-conversation"><header class="conversation-head"><div><span class="loop-kicker">Context · 固定 Revision 历史会话</span><h3>${escape(name)}</h3><p>${purpose}R${escape(conversation?.revision?.generation || node.revision?.generation || "—")} · ${escape(conversation?.total ?? "…")} 条消息</p></div><span class="context-run-state is-${escape(node.latest_run?.status || node.status)}">${escape(node.latest_run?.status || node.status)}</span></header>${executionActivity(node)}${userMessages(manifest, node.context_id)}<div class="context-metrics">${contextMetrics(node)}</div>${causality(state.causality)}<div class="conversation-tools"><input type="search" data-loop-message-search value="${escape(state.messageSearch)}" placeholder="搜索消息内容、工具调用、文件名…"><div class="conversation-filters">${filters.map(value => `<button type="button" data-action="loop-message-filter" data-filter="${value}" class="${state.messageFilter === value ? "is-active" : ""}">${value}</button>`).join("")}</div></div><div class="loop-transcript" data-loop-transcript data-context-id="${escape(node.context_id)}" data-range-start="${escape(conversation?.range?.start ?? 0)}">${historyControl}${messages.map(item => messageCard({ ...item, context_id: node.context_id }, false, conversation?.revision || node.revision)).join("") || localize('<p class="history-boundary">没有匹配的消息</p>')}${newerControl}</div>${composer}</section>`;
  }

  function renderExecutionActivity(state) {
    const node = state.manifest?.nodes?.find(item => item.context_id === state.selectedContextId);
    return node ? executionActivity(node) : "";
  }

  function renderCausality(state) {
    return causality(state.causality);
  }

  function renderInspection(conversation) {
    const messages = conversation?.messages || [];
    return localize`<section class="context-conversation is-inspection"><p>Context 检查 · 固定 Revision · ${escape(conversation?.total || 0)} 条消息</p><div class="loop-transcript">${messages.map(item => messageCard({ ...item, context_id: conversation.context_id || item.context_id }, true, conversation.revision)).join("") || localize("暂无消息")}</div>${conversation?.has_more ? localize`<button data-patrol-context-older>加载更早消息</button>` : ""}</section>`;
  }

  return Object.freeze({ render, renderExecutionActivity, renderCausality, renderInspection });
});
