/*
 * 本文件对外提供 Loop 中单个 Context 的完整会话、真实 directive 因果带与三类介入面板。
 * 输入为分页会话、选中节点、canonical causality、来源审计、筛选与 Loop 生命周期；输出为状态摘要、Patrol/用户来源分明的因果事件、完整消息和运行期命令表单。
 * 具体工作流为因果带只读取已提交事件，来源徽标保持在审计栏，正文不被元数据污染；会话维持有界双向分页，终止态只读。
 * 示例：`FocusContextConversationView.render(consoleState)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextConversationView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const role = message => String(message?.role || message?.type || "message").toLowerCase();
  const content = message => typeof message?.content === "string" ? message.content : JSON.stringify(message?.content ?? "", null, 2);

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

  function messageCard(item) {
    const message = item.message || {};
    const itemRole = role(message);
    const provenance = item.provenance;
    const compression = message.compression || message.additional_kwargs?.compression;
    const source = provenance ? `<span class="message-provenance" title="该信息仅用于 Focus 审计，不进入模型 Context">${escape(provenance.source_kind === "delegated_patrol" ? "Patrol delegated" : provenance.source_kind)}</span>` : itemRole === "human" ? '<span class="message-provenance is-direct">Direct human</span>' : "";
    const toolMeta = itemRole === "tool" ? `<small>${escape(message.name || "tool")} · ${escape(message.tool_call_id || "")}</small>` : "";
    const compressed = compression ? `<details class="compression-audit"><summary>${compression.deleted ? "已删除块" : "压缩块"} · 原始来源可恢复</summary><pre>${escape(JSON.stringify(compression.source || [], null, 2))}</pre>${message.id ? `<button type="button" class="text-button" data-action="loop-restore-compression" data-context-id="${escape(item.context_id || "")}" data-message-id="${escape(message.id)}">恢复原始消息并发布新 Revision</button>` : ""}</details>` : "";
    return `<article class="loop-message is-${escape(itemRole)}" data-message-index="${item.index}" data-message-role="${escape(itemRole)}"><header><strong>${escape(itemRole)}</strong>${source}<span>#${item.index + 1}</span></header><pre>${escape(content(message))}</pre>${toolMeta}${compressed}</article>`;
  }

  function contextMetrics(node) {
    const counts = node.counts || {};
    return [
      [counts.runs, "Runs"],
      [counts.delegated_messages, "Patrol 指令"],
      [counts.workspace_changes, "工作区变更"],
      [counts.artifacts, "产物"],
    ].map(([value, label]) => `<div><strong>${escape(value || 0)}</strong><span>${label}</span></div>`).join("");
  }

  function causality(entries) {
    if (!entries?.length) return "";
    const visible = entries.slice(-8);
    return `<section class="context-causality" aria-label="指令因果链"><span class="loop-kicker">Live causality</span><ol>${visible.map(item => `<li data-causality-id="${escape(item.event_id)}" class="is-${escape(item.entity_type)}"><i aria-hidden="true"></i><strong>${escape(item.entity_type === "directive" ? "Patrol directive" : item.entity_type)}</strong><span>${escape(item.summary)}</span><small>${escape(item.kind)}</small></li>`).join("")}</ol></section>`;
  }

  function render(state) {
    const manifest = state.manifest;
    const node = manifest?.nodes?.find(item => item.context_id === state.selectedContextId);
    const conversation = state.conversation;
    if (!node) return '<section class="context-conversation is-empty">选择一个 Context 查看完整会话</section>';
    const modes = [
      ["direct_context_message", "直接进入 Context", "以用户 HumanMessage 立即启动该 Context 的 Main Run"],
      ["patrol_context_intent", "告诉 Patrol：这个 Context", "意见进入 Patrol 下一次观察，不污染模型消息"],
      ["patrol_portfolio_intent", "告诉 Patrol：整体布局", "对所有 Context 的分工、保留或淘汰提出意见"],
    ];
    const mode = modes.find(item => item[0] === state.interventionMode) || modes[0];
    const messages = visibleMessages(state);
    const filters = ["all", "human", "assistant", "tool"];
    const historyControl = conversation?.has_more
      ? `<div class="history-sentinel" data-loop-history-sentinel aria-hidden="true"></div><button type="button" class="load-history" data-action="loop-load-older">加载更早消息（还有 ${conversation.range.start} 条）</button>`
      : '<p class="history-boundary">已到达该 revision 的会话起点</p>';
    const newerControl = conversation?.has_newer
      ? `<button type="button" class="load-history" data-action="loop-load-newer">加载更新消息（还有 ${conversation.total - conversation.range.end} 条）</button><div class="history-sentinel" data-loop-newer-sentinel aria-hidden="true"></div>`
      : '<p class="history-boundary">已到达该 revision 的最新消息</p>';
    const composer = state.terminal
      ? '<div class="loop-intervention is-readonly"><strong>历史只读</strong><span>该 Loop 已结束，退出后可在当前 Context 发送新的用户消息并创建后继 Loop。</span></div>'
      : `<form id="loopInterventionForm" class="loop-intervention"><div class="intervention-modes" role="tablist">${modes.map(item => `<button type="button" role="tab" data-action="loop-intervention-mode" data-mode="${item[0]}" aria-selected="${state.interventionMode === item[0]}" class="${state.interventionMode === item[0] ? "is-active" : ""}">${item[1]}</button>`).join("")}</div><p>${escape(mode[2])}</p><div class="intervention-composer"><textarea name="content" required rows="3" placeholder="${escape(mode[0] === "direct_context_message" ? "向这个 Context 发送新的 HumanMessage" : "表达你的意图，Patrol 将在下次判断中处理")}"></textarea><button class="primary" type="submit" ${state.pending === "intervention" ? "disabled" : ""}>${state.pending === "intervention" ? "提交中…" : "发送"}</button></div></form>`;
    return `<section class="context-conversation"><header class="conversation-head"><div><span class="loop-kicker">完整 Context 会话</span><h3>${escape(node.topic || node.title)}</h3><p>${escape(node.purpose)} · R${escape(conversation?.revision?.generation || node.revision?.generation || "—")} · ${escape(conversation?.total ?? "…")} 条消息</p></div><span class="context-run-state is-${escape(node.latest_run?.status || node.status)}">${escape(node.latest_run?.status || node.status)}</span></header><div class="context-metrics">${contextMetrics(node)}</div>${causality(state.causality)}<div class="conversation-tools"><input type="search" data-loop-message-search value="${escape(state.messageSearch)}" placeholder="搜索消息内容、工具调用、文件名…"><div class="conversation-filters">${filters.map(value => `<button type="button" data-action="loop-message-filter" data-filter="${value}" class="${state.messageFilter === value ? "is-active" : ""}">${value}</button>`).join("")}</div></div><div class="loop-transcript" data-loop-transcript data-context-id="${escape(node.context_id)}" data-range-start="${escape(conversation?.range?.start ?? 0)}">${historyControl}${messages.map(item => messageCard({ ...item, context_id: node.context_id })).join("") || '<p class="history-boundary">没有匹配的消息</p>'}${newerControl}</div>${composer}</section>`;
  }

  return Object.freeze({ render });
});
