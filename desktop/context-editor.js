(function contextEditorModule(global) {
  "use strict";

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function cloneMessages(messages) {
    return JSON.parse(JSON.stringify(Array.isArray(messages) ? messages : []));
  }

  function createUiKeys(messages, keyFactory) {
    return (Array.isArray(messages) ? messages : []).map(() => keyFactory());
  }

  function messagePreview(message) {
    const content = message && typeof message === "object" ? message.content : message;
    const text = typeof content === "string" ? content : JSON.stringify(content ?? "");
    return text.replace(/\s+/g, " ").trim().slice(0, 260) || "空内容";
  }

  function protocolSummary(message) {
    const details = [];
    if (message?.name) details.push(`name: ${message.name}`);
    if (message?.tool_call_id) details.push(`tool_call_id: ${message.tool_call_id}`);
    if (Array.isArray(message?.tool_calls)) details.push(`${message.tool_calls.length} 个 tool call`);
    return details.join(" · ");
  }

  function renderSourceMessage(message, index, contextId, locked) {
    const role = message && typeof message === "object" ? (message.role || "未指定角色") : "非法消息";
    return `<article class="context-source-message" data-context-source-index="${index}" data-source-context-id="${escapeHtml(contextId)}">
      <header><span><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(role)}</strong>${protocolSummary(message) ? `<small>${escapeHtml(protocolSummary(message))}</small>` : ""}</span><span class="context-message-actions">
        <button type="button" class="context-pointer-handle" data-context-pointer-handle data-context-drag-origin="source" aria-label="拖拽此来源消息到新 Context"${locked ? " disabled" : ""}>⠿</button>
        <button type="button" class="text-button" data-action="context-source-copy"${locked ? " disabled" : ""}>加入</button>
      </span></header>
      <p class="context-message-preview">${escapeHtml(messagePreview(message))}</p>
      <details class="context-source-json"><summary>查看完整 JSON</summary><pre>${escapeHtml(JSON.stringify(message, null, 2))}</pre></details>
    </article>`;
  }

  function renderSourceMessages(messages, contextId, locked = false) {
    return (Array.isArray(messages) ? messages : []).map((message, index) => renderSourceMessage(message, index, contextId, locked)).join("") || '<p class="context-empty-source muted">这个来源 checkpoint 没有消息。</p>';
  }

  function renderMessage(message, index, uiKey, expandedKey, locked) {
    const role = message && typeof message === "object" ? (message.role || "未指定角色") : "非法消息";
    const expanded = uiKey === expandedKey;
    return `<article class="context-message-editor${expanded ? " is-expanded" : ""}" data-context-message-index="${index}" data-context-ui-key="${escapeHtml(uiKey)}">
      <header><span><button type="button" class="context-pointer-handle" data-context-pointer-handle data-context-drag-origin="draft" aria-label="拖拽消息排序" aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"${locked ? " disabled" : ""}>⠿</button><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(role)}</strong>${protocolSummary(message) ? `<small>${escapeHtml(protocolSummary(message))}</small>` : ""}</span><span class="context-message-actions">
        <button type="button" class="text-button" data-action="context-message-copy"${locked ? " disabled" : ""}>复制</button>
        <button type="button" class="text-button danger" data-action="context-message-delete"${locked ? " disabled" : ""}>删除</button>
      </span></header>
      <button type="button" class="context-message-summary" data-action="context-message-toggle" aria-expanded="${expanded ? "true" : "false"}"><span class="context-message-preview">${escapeHtml(messagePreview(message))}</span><span>${expanded ? "收起" : "编辑 JSON"}</span></button>
      ${expanded ? `<textarea data-context-message-json data-context-message-index="${index}" spellcheck="false" aria-label="消息 ${index + 1} JSON"${locked ? " disabled" : ""}>${escapeHtml(JSON.stringify(message, null, 2))}</textarea>` : ""}
    </article>`;
  }

  function renderMessages(messages, uiKeys = [], expandedKey = null, locked = false) {
    return messages.map((message, index) => renderMessage(message, index, uiKeys[index] || `context-ui-${index}`, expandedKey, locked)).join("") || '<div class="context-empty-drop"><strong>把左侧消息拖到这里</strong><span>也可以直接新增任意消息</span></div>';
  }

  function readMessages(root, currentMessages = []) {
    const messages = cloneMessages(currentMessages);
    [...root.querySelectorAll("[data-context-message-json]")].forEach((input, position) => {
      const index = Number(input.dataset.contextMessageIndex ?? position);
      let value;
      try { value = JSON.parse(input.value); }
      catch (error) { throw new Error(`消息 ${index + 1} 不是合法 JSON：${error.message}`); }
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(`消息 ${index + 1} 必须是 JSON 对象`);
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
      const walk = contextId => {
        if (seen.has(contextId)) return;
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

  function rootContextId(nodes, contextId) {
    let currentId = contextId;
    const visited = new Set();
    while (!visited.has(currentId)) {
      visited.add(currentId);
      const parentId = nodes.get(currentId)?.parents?.[0]?.context_id;
      if (!parentId) return currentId;
      currentId = parentId;
    }
    return [...visited].sort()[0];
  }

  function contextFamilyTasks(tasks, trees, activeTaskId) {
    const taskList = Array.isArray(tasks) ? tasks : [];
    const activeTask = taskList.find(task => task.task_id === activeTaskId);
    if (!activeTask) return [];
    const tree = trees instanceof Map ? trees.get(activeTask.workspace_id) : null;
    const nodes = new Map((Array.isArray(tree) ? tree : []).map(node => [node.context_id, node]));
    const rootId = rootContextId(nodes, activeTaskId);
    return orderTasksByTree(taskList, trees).filter(task =>
      task.workspace_id === activeTask.workspace_id && rootContextId(nodes, task.task_id) === rootId
    );
  }

  global.FocusContextEditor = { cloneMessages, contextFamilyTasks, createUiKeys, move, orderTasksByTree, readMessages, renderMessages, renderSourceMessages };
})(window);
