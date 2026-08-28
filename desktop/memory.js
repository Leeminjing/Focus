/*
 * 本文件对外提供记忆库的纯渲染器。输入为记忆库列表与整理态状态，
 * 输出为记忆库工作台 HTML；工作流只呈现记忆实体与三栏整理器，不改变记忆持久化语义。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMemoryView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  // 布局比例（上下板 / 左右编辑区）统一钳制到合法区间，避免渲染出 <0 或 >1 的 flex。
  function clampRatio(value, min, max) {
    const n = Number(value);
    if (!Number.isFinite(n)) return min;
    return Math.min(max, Math.max(min, n));
  }

  // 分段记忆目标区：每段一个可编辑卡（标题 + 摘要正文）。
  function renderSegments(segments) {
    const items = (segments || []).map((seg, index) => {
      const title = seg?.title || `来源 ${index + 1}`;
      const body = seg?.body || "";
      return `<article class="memory-segment" data-segment-index="${index}">
        <header class="memory-segment-head">
          <span class="memory-segment-label">${index + 1}</span>
          <input class="memory-segment-title" data-segment-field="title" data-segment-index="${index}" placeholder="段标题" value="${escapeHtml(title)}">
        </header>
        <textarea class="memory-segment-body" data-segment-field="body" data-segment-index="${index}" rows="4" placeholder="本段压缩摘要（可编辑）">${escapeHtml(body)}</textarea>
      </article>`;
    }).join("");
    return items || '<p class="memory-empty-edit muted">切换「分段记忆」后点「重新压缩」，每段来源会压缩成独立记忆段。</p>';
  }

  const KIND_LABELS = { full_session: "完整会话", partial_messages: "部分消息", partial_text: "部分文字", manual: "手动输入", merged: "合并来源" };

  function roleLabel(role) {
    return { human: "用户", user: "用户", ai: "助手", assistant: "助手", tool: "工具", system: "系统" }[role] || role || "消息";
  }

  function messageText(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.map(item => typeof item === "string" ? item : (item && item.text) || "").join("\n");
    return String(content || "");
  }

  function messagePreview(content) {
    const text = messageText(content).replace(/\s+/g, " ").trim();
    if (!text) return "（空）";
    return text.length > 140 ? text.slice(0, 140) + "…" : text;
  }

  function isDialogueRole(role) {
    return role === "human" || role === "user" || role === "ai" || role === "assistant";
  }

  // === 浏览态：左记忆列表，右单条详情 ===

  function card(memory, selected) {
    const kind = KIND_LABELS[memory.source_kind] || memory.source_kind || "手动输入";
    return `<button type="button" class="memory-card${selected ? " is-selected" : ""}" data-action="select-memory" data-memory-id="${escapeHtml(memory.memory_id)}" aria-pressed="${selected}">
      <span class="memory-card-heading"><strong>${escapeHtml(memory.title)}</strong><span class="ui-badge is-neutral">${escapeHtml(kind)}</span></span>
      <span class="memory-card-content">${escapeHtml(memory.content)}</span>
    </button>`;
  }

  function detail(memory) {
    const kind = KIND_LABELS[memory.source_kind] || memory.source_kind || "手动输入";
    return `<section class="memory-detail-card">
      <header class="memory-detail-heading">
        <div><span class="workspace-kicker">MEMORY</span><h2>${escapeHtml(memory.title)}</h2><p class="muted">${escapeHtml(kind)} · ${escapeHtml((memory.updated_at || "").slice(0, 10)) || ""}</p></div>
        <span class="ui-badge is-neutral">${escapeHtml(kind)}</span>
      </header>
      <pre class="memory-detail-content">${escapeHtml(memory.content)}</pre>
      <div class="memory-detail-actions">
        <button class="text-button" type="button" data-action="edit-memory" data-memory-id="${escapeHtml(memory.memory_id)}">编辑</button>
        <button class="text-button danger" type="button" data-action="delete-memory" data-memory-id="${escapeHtml(memory.memory_id)}">删除</button>
      </div>
    </section>`;
  }

  function workbench(memories, options) {
    const all = memories || [];
    const selected = all.find(m => m.memory_id === options.selectedId) || all[0] || null;
    return `<div class="memory-workbench">
      <aside class="memory-list"><header><strong>记忆</strong><span class="memory-list-actions"><button type="button" class="text-button" data-action="new-memory">新建</button><span>${all.length}</span></span></header>
        <div class="memory-list-scroll">${all.map(m => card(m, m === selected)).join("") || `<section class="ui-empty-state"><h1>尚无记忆</h1><p>新建一条，沉淀跨会话知识。</p></section>`}</div>
      </aside>
      <main class="memory-detail">${selected ? detail(selected) : '<section class="ui-empty-state"><h1>选择一条记忆</h1><p>查看、编辑或删除。</p></section>'}</main>
    </div>`;
  }

  // === 整理态：三栏 + 记忆框 ===

  function compose(state) {
    // 第一栏：会话列表（按工作区分组，会话卡可拖拽 = 完整会话来源）
    const groups = new Map();
    for (const s of (state.sessions || [])) {
      const key = s.workspace_name || "本地工作区";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    }
    // 分组展开状态：显式展开的 + 选中会话所在分组（恒展开）。state 驱动，重建后恢复。
    const expanded = new Set(state.expandedGroups || []);
    const activeWorkspace = (state.sessions || []).find(s => s.task_id === state.activeSessionId)?.workspace_name || "本地工作区";
    if (state.activeSessionId) expanded.add(activeWorkspace);
    const sessions = [...groups.entries()].map(([workspace, items]) => `
      <details class="memory-session-group" data-group="${escapeHtml(workspace)}"${expanded.has(workspace) ? " open" : ""}>
        <summary class="memory-session-group-head" data-action="toggle-memory-group" data-group="${escapeHtml(workspace)}"><span class="memory-session-group-name">${escapeHtml(workspace)}</span><span class="memory-session-group-count">${items.length}</span></summary>
        <div class="memory-session-group-body">
          ${items.map(s => `
            <button type="button" class="memory-session-item${state.activeSessionId === s.task_id ? " is-active" : ""}" draggable="true" data-action="pick-session" data-context-id="${escapeHtml(s.task_id)}" data-context-title="${escapeHtml(s.title)}">
              <span class="memory-session-title">${escapeHtml(s.title)}</span>
              <span class="memory-session-meta">${s.thread_id ? escapeHtml(s.thread_id.slice(0, 8)) : ""}</span>
            </button>`).join("")}
        </div>
      </details>`).join("") || '<p class="muted">暂无可选会话。</p>';

    // 第二栏：对话消息（勾选多选不连续，消息卡可拖拽 = 选中的消息来源）
    const dialogue = (state.enabledMessages || []).filter(m => isDialogueRole(m.role) && messageText(m.content).trim());
    const selectedIds = new Set(state.selectedMessageIds || []);
    const list = dialogue.map((message, index) => {
      const id = message.id || `idx-${index}`;
      const checked = selectedIds.has(id);
      const active = state.activeMessageId === id;
      return `<button type="button" class="memory-msg${active ? " is-active" : ""}${checked ? " is-checked" : ""}" draggable="true" data-action="toggle-message" data-message-id="${escapeHtml(id)}" aria-pressed="${checked}">
        <div class="memory-msg-head"><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(roleLabel(message.role))}</strong><span class="memory-msg-check">${checked ? "✓" : "+"}</span></div>
        <p class="memory-msg-preview">${escapeHtml(messagePreview(message.content))}</p>
      </button>`;
    }).join("");
    const listEmpty = '<p class="muted">点击左侧会话，选择要整理的对话。</p>';

    // 第三栏：当前消息全文（可划选文字高亮 = 部分文字来源，可拖拽）
    const sel = (state.enabledMessages || []).find(m => (m.id || "") === state.activeMessageId) || null;
    const source = sel
      ? `<article class="memory-source" data-message-id="${escapeHtml(sel.id)}">
          <div class="memory-source-head"><strong>${escapeHtml(roleLabel(sel.role))}</strong><button type="button" class="text-button" data-action="add-text">加入选中文字</button></div>
          <pre class="memory-source-text" data-selectable-text>${escapeHtml(messageText(sel.content))}</pre>
        </article>`
      : '<p class="muted">在左侧消息栏点选一条，查看完整原文并划选文字。</p>';

    // 记忆编辑区：左「记忆源·原始」(可编辑原文 + 拖拽重排 + 多选合并) + 右「压缩后的记忆」(可编辑)
    const sourceItems = (state.collectedSources || []).map((source, index) => {
      const kind = KIND_LABELS[source.kind] || source.kind;
      // 各 kind 组内序号，便于区分同 kind 多个来源。
      const sameKindIndex = (state.collectedSources || []).slice(0, index).filter(s => s.kind === source.kind).length + 1;
      const sameKindTotal = (state.collectedSources || []).filter(s => s.kind === source.kind).length;
      const kindOrdinal = sameKindTotal > 1 ? `（${sameKindIndex}/${sameKindTotal}）` : "";
      const label = source.kind === "session" ? `${escapeHtml(source.title || "会话")}`
        : source.kind === "messages" ? `${source.count || 0} 条`
        : source.kind === "text" ? `划选文字${source.ranges?.length > 1 ? ` · ${source.ranges.length} 段` : ""}`
        : source.kind === "merged" ? `${(source.subSources || []).length} 项合并`
        : escapeHtml(source.title || "");
      const isMergeSelected = (state.selectedSourcesForMerge || []).includes(index);
      // 拖拽重排仅在非合并模式下启用，避免与点选合并冲突。
      const dragAttr = state.mergeMode ? "" : ` draggable="true" title="拖动可调整合并顺序"`;
      const cardClass = `memory-source-edit${state.mergeMode ? " is-mergeable" : ""}${isMergeSelected ? " is-merge-selected" : ""}`;
      const cardAction = state.mergeMode ? ` data-action="toggle-merge-source" data-source-index="${index}"` : "";
      const removeBtn = state.mergeMode ? "" : `<button type="button" class="text-button danger" data-action="remove-collected" data-collected-index="${index}">×</button>`;
      return `<article class="${cardClass}" data-source-index="${index}"${cardAction}>
        <header class="memory-source-edit-head"${dragAttr}>
          <strong>${index + 1}. ${escapeHtml(kind)}${kindOrdinal}</strong>
          <span class="memory-source-edit-label">${label}</span>
          ${state.mergeMode ? `<span class="memory-source-merge-check">${isMergeSelected ? "✓" : "+"}</span>` : removeBtn}
        </header>
        <textarea class="memory-source-edit-text" data-source-index="${index}" data-source-field="rawText" rows="4" placeholder="原始内容（可编辑）"${state.mergeMode ? " readonly" : ""}>${escapeHtml(source.rawText || source.text || "")}</textarea>
      </article>`;
    }).join("") || '<p class="memory-empty-edit muted">在上方选择会话 / 勾选消息 / 划选文字，添加记忆源。</p>';

    const editorRatio = clampRatio(state.editorRatio, 0.15, 0.85);
    const sourceRatio = clampRatio(state.sourceRatio, 0.2, 0.8);

    return `<div class="memory-compose">
      <div class="memory-compose-top" data-memory-top style="flex:${editorRatio} 1 0">
        <div class="memory-compose-grid">
          <aside class="memory-pane">
            <header><strong>对话</strong><span>${(state.sessions || []).length}</span></header>
            <div class="memory-session-list">${sessions}</div>
          </aside>
          <main class="memory-pane">
            <header><strong>消息</strong><span class="memory-pane-actions"><button type="button" class="text-button" data-action="add-checked-messages">加入勾选</button><span>${dialogue.length}</span></span></header>
            <div class="memory-msg-list">${list || listEmpty}</div>
          </main>
          <main class="memory-pane">
            <header><strong>原文</strong><span>${sel ? escapeHtml(roleLabel(sel.role)) : "未选"}</span></header>
            <div class="memory-source-wrap">${source}</div>
          </main>
        </div>
      </div>
      <div class="memory-resizer memory-resizer-v" data-memory-resizer-vertical role="separator" aria-orientation="horizontal"></div>
      <div class="memory-compose-bottom" data-memory-bottom style="flex:${1 - editorRatio} 1 0">
        <div class="memory-editor" data-memory-editor>
          <section class="memory-editor-sources" style="flex:${sourceRatio} 1 0">
            <header class="memory-editor-head">
              <strong>记忆源 · 原始完整视图</strong>
              <span class="memory-source-merge-actions">
                ${state.mergeMode
                  ? `<button type="button" class="text-button" data-action="confirm-merge-sources">合并所选</button>
                     <button type="button" class="text-button" data-action="cancel-merge-sources">取消</button>`
                  : `<button type="button" class="text-button" data-action="enter-merge-sources" ${(state.collectedSources || []).length < 2 ? "disabled" : ""}>合并</button>`}
              </span>
              <span>${(state.collectedSources || []).length}</span>
            </header>
            <div class="memory-source-edit-list${state.mergeMode ? " is-merge-mode" : ""}">${sourceItems}</div>
          </section>
          <div class="memory-resizer memory-resizer-h" data-memory-resizer-horizontal role="separator" aria-orientation="vertical"></div>
          <section class="memory-editor-target" style="flex:${1 - sourceRatio} 1 0">
            <header class="memory-editor-head">
              <strong>压缩后的记忆 · 完整视图</strong>
              <span class="memory-mode-toggle" role="group" aria-label="压缩语义">
                <button type="button" class="text-button${state.contentMode !== "segmented" ? " is-active" : ""}" data-action="set-memory-mode" data-mode="complete">完整记忆</button>
                <button type="button" class="text-button${state.contentMode === "segmented" ? " is-active" : ""}" data-action="set-memory-mode" data-mode="segmented">分段记忆</button>
              </span>
              <button type="button" class="text-button" data-action="recompress-memory">重新压缩</button>
            </header>
            <input class="memory-title-input" data-memory-field="title" placeholder="记忆标题" value="${escapeHtml(state.draftTitle || "")}">
            ${state.contentMode === "segmented" ? renderSegments(state.segments) : `<textarea class="memory-content-input" data-memory-field="content" placeholder="压缩总结后的完整记忆（可编辑）" rows="8">${escapeHtml(state.draftContent || "")}</textarea>`}
          </section>
        </div>
        <div class="memory-create-actions">
          <button class="primary" type="button" data-action="save-memory">保存记忆</button>
          <button class="text-button" type="button" data-action="cancel-compose">取消</button>
        </div>
      </div>
    </div>`;
  }

  function render(memories, options = {}) {
    return `<section class="memory-view">
      ${options.composing ? compose(options) : workbench(memories, options)}
    </section>`;
  }

  return { render, messageText, messagePreview, roleLabel, isDialogueRole };
});
