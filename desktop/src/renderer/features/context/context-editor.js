(function contextEditorModule(global) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function cloneMessages(messages) {
    return JSON.parse(JSON.stringify(Array.isArray(messages) ? messages : []));
  }

  function createUiKeys(messages, keyFactory) {
    return (Array.isArray(messages) ? messages : []).map(() => keyFactory());
  }

  function textParts(message) {
    if (typeof message?.content === "string") return [{ value: message.content }];
    if (!Array.isArray(message?.content)) return [];
    return message.content.flatMap((part, index) =>
      part && ["text", "input_text", "output_text"].includes(part.type) && typeof part.text === "string"
        ? [{ value: part.text, index }] : []
    );
  }

  function messagePreview(message) {
    const parts = textParts(message);
    const text = parts.map(part => part.value).join("\n");
    const preview = text.replace(/\s+/g, " ").trim().slice(0, 260);
    const content = message?.content;
    const structured = Array.isArray(content) ? content.length > parts.length : content != null && typeof content !== "string";
    return [preview, structured ? localize("含非文本内容") : ""].filter(Boolean).join(" · ") || localize("空内容");
  }

  function protocolSummary(message) {
    const details = [];
    if (message?.name) details.push(`name: ${message.name}`);
    if (message?.tool_call_id) details.push(`tool_call_id: ${message.tool_call_id}`);
    if (Array.isArray(message?.tool_calls)) details.push(localize`${message.tool_calls.length} 个 tool call`);
    return details.join(" · ");
  }

  function renderDetails(message, index, editable, editingJson = false, locked = false) {
    const summary = protocolSummary(message);
    return localize`<details class="context-source-json"${editingJson ? " open" : ""}><summary>结构详情</summary>
      ${summary ? `<p>${escapeHtml(summary)}</p>` : ""}
      ${editable ? localize`<button type="button" class="text-button" data-action="context-message-edit-json" aria-expanded="${editingJson ? "true" : "false"}"${locked ? " disabled" : ""}>${editingJson ? localize("收起编辑") : localize("编辑完整 JSON")}</button>` : ""}
      ${editingJson ? localize`<textarea data-context-message-json data-context-message-index="${index}" spellcheck="false" aria-label="消息 ${index + 1} JSON"${locked ? " disabled" : ""}>${escapeHtml(JSON.stringify(message, null, 2))}</textarea>` : localize`<details><summary>查看完整 JSON</summary><pre>${escapeHtml(JSON.stringify(message, null, 2))}</pre></details>`}
    </details>`;
  }

  function renderSourceMessage(message, index, contextId, locked) {
    const role = message && typeof message === "object" ? (message.role || localize("未指定角色")) : localize("非法消息");
    return localize`<article class="context-source-message" data-context-source-index="${index}" data-source-context-id="${escapeHtml(contextId)}">
      <header><span><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(role)}</strong></span><span class="context-message-actions">
        <button type="button" class="context-pointer-handle" data-context-pointer-handle data-context-drag-origin="source" aria-label="拖拽此来源消息到新上下文"${locked ? " disabled" : ""}><span class="ui-icon is-sm icon-grip-vertical" aria-hidden="true"></span></button>
        <button type="button" class="text-button" data-action="context-source-copy"${locked ? " disabled" : ""}>加入</button>
      </span></header>
      <p class="context-message-preview">${escapeHtml(messagePreview(message))}</p>
      ${renderDetails(message, index, false)}
    </article>`;
  }

  function renderSourceMessages(messages, contextId, locked = false) {
    return (Array.isArray(messages) ? messages : []).map((message, index) => renderSourceMessage(message, index, contextId, locked)).join("") || localize('<p class="context-empty-source muted">这个来源 checkpoint 没有消息。</p>');
  }

  function renderMessage(message, index, uiKey, expandedKey, locked, editMode) {
    const role = message && typeof message === "object" ? (message.role || localize("未指定角色")) : localize("非法消息");
    const expanded = uiKey === expandedKey;
    const editingJson = expanded && editMode === "json";
    const editingBody = expanded && !editingJson;
    const parts = textParts(message);
    return localize`<article class="context-message-editor${expanded ? " is-expanded" : ""}" data-context-message-index="${index}" data-context-ui-key="${escapeHtml(uiKey)}">
      <header><span><button type="button" class="context-pointer-handle" data-context-pointer-handle data-context-drag-origin="draft" aria-label="拖拽消息排序" aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"${locked ? " disabled" : ""}><span class="ui-icon is-sm icon-grip-vertical" aria-hidden="true"></span></button><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(role)}</strong></span><span class="context-message-actions">
        <button type="button" class="text-button" data-action="context-message-duplicate"${locked ? " disabled" : ""}>创建副本</button>
        <button type="button" class="text-button danger" data-action="context-message-delete"${locked ? " disabled" : ""}>删除</button>
      </span></header>
      ${parts.length ? localize`<button type="button" class="context-message-summary" data-action="context-message-toggle" aria-expanded="${editingBody ? "true" : "false"}"><span class="context-message-preview">${escapeHtml(messagePreview(message))}</span><span>${editingBody ? localize("收起编辑") : localize("编辑正文")}</span></button>` : `<p class="context-message-preview">${escapeHtml(messagePreview(message))}</p>`}
      ${editingBody ? parts.map(part => `<textarea data-context-message-content data-context-message-index="${index}"${part.index == null ? "" : ` data-context-part-index="${part.index}"`} aria-label="${escapeHtml(part.index == null ? localize`消息 ${index + 1} 正文` : localize`消息 ${index + 1} 正文 ${part.index + 1}`)}"${locked ? " disabled" : ""}>${escapeHtml(part.value)}</textarea>`).join("") : ""}
      ${renderDetails(message, index, true, editingJson, locked)}
    </article>`;
  }

  function renderMessages(messages, uiKeys = [], expandedKey = null, locked = false, editMode = "body") {
    return messages.map((message, index) => renderMessage(message, index, uiKeys[index] || `context-ui-${index}`, expandedKey, locked, editMode)).join("") || localize('<div class="context-empty-drop"><strong>从“来源”加入消息</strong><span>点击“加入”或拖放到此处，也可以新增消息。</span></div>');
  }

  function readMessages(root, currentMessages = []) {
    const messages = cloneMessages(currentMessages);
    [...root.querySelectorAll("[data-context-message-content]")].forEach(input => {
      const message = messages[Number(input.dataset.contextMessageIndex)];
      if (input.dataset.contextPartIndex == null) message.content = input.value;
      else message.content[Number(input.dataset.contextPartIndex)].text = input.value;
    });
    [...root.querySelectorAll("[data-context-message-json]")].forEach((input, position) => {
      const index = Number(input.dataset.contextMessageIndex ?? position);
      let value;
      try { value = JSON.parse(input.value); }
      catch (error) { throw new Error(localize`消息 ${index + 1} 不是合法 JSON：${error.message}`); }
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(localize`消息 ${index + 1} 必须是 JSON 对象`);
      messages[index] = value;
    });
    return messages;
  }

  function move(messages, from, to) {
    if (from < 0 || from >= messages.length || to < 0 || to >= messages.length || from === to) return;
    messages.splice(to, 0, messages.splice(from, 1)[0]);
  }

  function orderTasksByTree(tasks, trees) {
    const taskList = Array.isArray(tasks) ? tasks : [];
    const ordered = [];
    const seen = new Set();
    const workspaces = [...new Set(taskList.map(task => task.workspace_id))];
    const append = task => {
      if (task && !seen.has(task.task_id)) {
        seen.add(task.task_id);
        ordered.push(task);
      }
    };

    workspaces.forEach(workspaceId => {
      const tree = trees instanceof Map ? trees.get(workspaceId) : null;
      const nodes = Array.isArray(tree) ? tree : [];
      const byId = new Map(taskList.filter(task => task.workspace_id === workspaceId).map(task => [task.task_id, task]));
      const nodeIds = new Set(nodes.map(node => node.context_id));
      const children = new Map();
      const roots = [];
      nodes.forEach(node => {
        const parentId = node.parents?.[0]?.context_id;
        if (parentId && nodeIds.has(parentId)) {
          if (!children.has(parentId)) children.set(parentId, []);
          children.get(parentId).push(node.context_id);
        } else roots.push(node.context_id);
      });
      const visited = new Set();
      const walk = contextId => {
        if (visited.has(contextId)) return;
        visited.add(contextId);
        append(byId.get(contextId));
        (children.get(contextId) || []).forEach(walk);
      };
      roots.forEach(walk);
      nodes.forEach(node => walk(node.context_id));
      taskList.filter(task => task.workspace_id === workspaceId).forEach(append);
    });
    taskList.forEach(append);
    return ordered;
  }

  function contextFamilyTasks(tasks, trees, activeTaskId) {
    const taskList = Array.isArray(tasks) ? tasks : [];
    const activeTask = taskList.find(task => task.task_id === activeTaskId);
    if (!activeTask) return [];
    const tree = trees instanceof Map ? trees.get(activeTask.workspace_id) : null;
    const nodes = Array.isArray(tree) ? tree : [];
    const byId = new Map(taskList.filter(task => task.workspace_id === activeTask.workspace_id).map(task => [task.task_id, task]));
    const relations = new Map();
    for (const node of nodes) {
      // Deleted sources may be absent from the active task list. Keep their
      // identity and lifecycle from the lineage response for navigation notes.
      if (!byId.has(node.context_id) && node.lifecycle && node.lifecycle !== "active") {
        byId.set(node.context_id, { ...node, task_id: node.context_id, workspace_id: activeTask.workspace_id });
      }
      for (const parent of node.parents || []) {
        for (const [from, to] of [[node.context_id, parent.context_id], [parent.context_id, node.context_id]]) {
          if (!relations.has(from)) relations.set(from, new Set());
          relations.get(from).add(to);
        }
      }
    }
    // Family membership follows every source edge. First-parent ordering is
    // only a reading order; it must not discard cross-branch merge relations.
    const related = new Set();
    const pending = [activeTaskId];
    while (pending.length) {
      const id = pending.pop();
      if (related.has(id)) continue;
      related.add(id);
      pending.push(...(relations.get(id) || []));
    }
    return orderTasksByTree([...byId.values()], trees).filter(task => related.has(task.task_id));
  }

  global.FocusContextEditor = { cloneMessages, contextFamilyTasks, createUiKeys, move, orderTasksByTree, readMessages, renderMessage, renderMessages, renderSourceMessages };
})(window);
