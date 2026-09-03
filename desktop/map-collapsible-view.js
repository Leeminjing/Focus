/*
 * 本文件对外提供全图折叠视图的纯模型、HTML 渲染与键盘导航函数。输入为任务、Context tree、
 * 当前项和展开/选择状态，输出为唯一 active Context 树、可访问标记与导航动作；工作流以首个
 * 有效 parent 建立规范路径，透明跳过非 active 祖先，并把业务副作用留给 app.js。示例：
 * `FocusMapCollapsibleView.render(FocusMapCollapsibleView.buildModel(tasks, trees), options)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMapCollapsibleView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function lifecycle(item) {
    return item?.lifecycle || "active";
  }

  function validParent(node, nodesById) {
    return (node?.parents || []).find(parent => nodesById.has(parent.context_id))?.context_id || null;
  }

  function rootIdFor(contextId, canonicalParents) {
    let current = contextId;
    const trail = [];
    const seen = new Set();
    while (current && !seen.has(current)) {
      seen.add(current);
      trail.push(current);
      const parent = canonicalParents.get(current);
      if (!parent) return current;
      current = parent;
    }
    return trail.slice().sort()[0] || contextId;
  }

  function buildWorkspace(workspaceId, tasks, tree) {
    const nodes = Array.isArray(tree) ? tree : [];
    const nodesById = new Map(nodes.map(node => [node.context_id, node]));
    const tasksById = new Map(tasks.map(task => [task.task_id, task]));
    const canonicalParents = new Map(nodes.map(node => [node.context_id, validParent(node, nodesById)]));
    tasks.forEach(task => {
      if (!canonicalParents.has(task.task_id)) canonicalParents.set(task.task_id, null);
    });

    const roots = new Map();
    tasks.forEach(task => {
      const rootId = rootIdFor(task.task_id, canonicalParents);
      if (!roots.has(rootId)) roots.set(rootId, []);
      roots.get(rootId).push(task);
    });

    const rootGroups = [...roots.entries()].map(([rootId, rootTasks]) => {
      const activeTasks = rootTasks.filter(task => lifecycle(task) === "active");
      const activeIds = new Set(activeTasks.map(task => task.task_id));
      const visibleParents = new Map();
      activeTasks.forEach(task => {
        let parentId = canonicalParents.get(task.task_id);
        const seen = new Set([task.task_id]);
        while (parentId && !activeIds.has(parentId) && !seen.has(parentId)) {
          seen.add(parentId);
          parentId = canonicalParents.get(parentId);
        }
        visibleParents.set(task.task_id, parentId && activeIds.has(parentId) && !seen.has(parentId) ? parentId : null);
      });

      const children = new Map();
      activeTasks.forEach(task => {
        const parentId = visibleParents.get(task.task_id);
        if (!parentId) return;
        if (!children.has(parentId)) children.set(parentId, []);
        children.get(parentId).push(task.task_id);
      });

      const visibleNodes = new Map(activeTasks.map(task => {
        const node = nodesById.get(task.task_id);
        const canonicalParentId = canonicalParents.get(task.task_id);
        const additionalParents = (node?.parents || []).filter(parent => parent.context_id !== canonicalParentId);
        return [task.task_id, {
          id: task.task_id,
          task,
          node,
          parentId: visibleParents.get(task.task_id),
          childIds: children.get(task.task_id) || [],
          additionalParents,
        }];
      }));

      const rootTask = tasksById.get(rootId) || rootTasks[0] || null;
      const rootNode = nodesById.get(rootId);
      return {
        id: rootId,
        title: rootTask?.title || rootNode?.title || rootId,
        lifecycle: lifecycle(rootTask || rootNode),
        activeCount: activeTasks.length,
        topIds: activeTasks.filter(task => !visibleParents.get(task.task_id)).map(task => task.task_id),
        nodes: visibleNodes,
      };
    });

    return {
      id: workspaceId,
      name: tasks[0]?.workspace_name || "本地工作区",
      activeCount: rootGroups.reduce((total, group) => total + group.activeCount, 0),
      roots: rootGroups,
    };
  }

  function buildModel(tasks, contextTrees) {
    const taskList = Array.isArray(tasks) ? tasks : [];
    const workspaceIds = [...new Set(taskList.map(task => task.workspace_id))];
    const treeFor = workspaceId => contextTrees instanceof Map
      ? contextTrees.get(workspaceId)
      : contextTrees?.[workspaceId];
    const workspaces = workspaceIds.map(workspaceId => buildWorkspace(
      workspaceId,
      taskList.filter(task => task.workspace_id === workspaceId),
      treeFor(workspaceId),
    ));
    const contexts = new Map();
    workspaces.forEach(workspace => workspace.roots.forEach(group => {
      group.nodes.forEach(node => contexts.set(node.id, { ...node, workspaceId: workspace.id, rootId: group.id }));
    }));
    return { workspaces, contexts };
  }

  function expansionForTask(model, taskId) {
    const current = model?.contexts?.get(taskId);
    if (!current) return { workspaceId: null, contextIds: [] };
    const contextIds = [];
    const seen = new Set([taskId]);
    let parentId = current.parentId;
    while (parentId && !seen.has(parentId)) {
      seen.add(parentId);
      contextIds.unshift(parentId);
      parentId = model.contexts.get(parentId)?.parentId || null;
    }
    return { workspaceId: current.workspaceId, contextIds };
  }

  function itemKey(kind, id) {
    return `${kind}:${id}`;
  }

  function contextIsVisible(model, options, contextId) {
    const node = model.contexts.get(contextId);
    if (!node || !options.expandedWorkspaceIds.has(node.workspaceId)) return false;
    const seen = new Set([contextId]);
    let parentId = node.parentId;
    while (parentId && !seen.has(parentId)) {
      if (!options.expandedContextIds.has(parentId)) return false;
      seen.add(parentId);
      parentId = model.contexts.get(parentId)?.parentId || null;
    }
    return true;
  }

  function defaultFocusKey(model, options) {
    if (options.focusKey?.startsWith("workspace:") && model.workspaces.some(workspace => itemKey("workspace", workspace.id) === options.focusKey)) return options.focusKey;
    if (options.focusKey?.startsWith("context:") && contextIsVisible(model, options, options.focusKey.slice("context:".length))) return options.focusKey;
    if (options.activeTaskId && contextIsVisible(model, options, options.activeTaskId)) return itemKey("context", options.activeTaskId);
    return model.workspaces[0] ? itemKey("workspace", model.workspaces[0].id) : "";
  }

  function renderToggle(kind, id, expanded, label) {
    return `<button type="button" class="map-collapsible-toggle" data-action="toggle-map-${kind}" data-${kind}-id="${escapeHtml(id)}" aria-expanded="${expanded}" aria-label="${expanded ? "收起" : "展开"}${escapeHtml(label)}" tabindex="-1"><span class="ui-icon is-sm icon-chevron-right" aria-hidden="true"></span></button>`;
  }

  function renderNode(group, nodeId, level, parentKey, options, focusKey, seen) {
    if (seen.has(nodeId)) return "";
    seen.add(nodeId);
    const node = group.nodes.get(nodeId);
    if (!node) return "";
    const task = node.task;
    const children = node.childIds.filter(id => group.nodes.has(id));
    const hasChildren = children.length > 0;
    const expanded = hasChildren && options.expandedContextIds.has(nodeId);
    const selected = options.selectedContextIds.has(nodeId);
    const current = nodeId === options.activeTaskId;
    const key = itemKey("context", nodeId);
    const action = options.selectionMode ? "toggle-select-session" : "task-card";
    const status = options.presentStatus(task.active_run?.status || "ready");
    const sourceNames = node.additionalParents.map(parent => {
      const parentTask = options.tasksById.get(parent.context_id);
      return parentTask?.title || parent.context_id;
    });
    const sourceDetails = sourceNames.length ? `<details class="map-collapsible-sources"><summary aria-label="${escapeHtml(`${task.title} 还有 ${sourceNames.length} 个来源`)}">+${sourceNames.length} 来源</summary><span>${escapeHtml(sourceNames.join("、"))}</span></details>` : "";
    const selectedMark = options.selectionMode ? `<span class="map-collapsible-check" aria-hidden="true">${selected ? "✓" : ""}</span>` : "";
    const currentMark = current ? '<span class="map-collapsible-current">当前</span>' : "";
    const projection = node.node?.projection_status;
    const projectionMark = projection && !["root", "valid", "repaired", "approved"].includes(projection)
      ? `<span class="map-collapsible-projection">${escapeHtml(projection)}</span>` : "";
    const actions = options.selectionMode ? "" : `<details class="map-collapsible-actions">
      <summary aria-label="打开 ${escapeHtml(task.title)} 的操作" title="更多操作">···</summary>
      <div class="map-collapsible-menu">
        <button type="button" class="text-button" data-action="archive-context" data-context-id="${escapeHtml(nodeId)}">归档</button>
        <button type="button" class="text-button" data-action="cascade-archive-context" data-context-id="${escapeHtml(nodeId)}">级联归档</button>
        <button type="button" class="text-button danger" data-action="delete-context" data-context-id="${escapeHtml(nodeId)}">删除</button>
        <button type="button" class="text-button danger" data-action="cascade-delete-context" data-context-id="${escapeHtml(nodeId)}">级联删除</button>
        <dl><div><dt>Context ID</dt><dd>${escapeHtml(nodeId)}</dd></div><div><dt>Thread</dt><dd>${escapeHtml(task.thread_id)}</dd></div><div><dt>路径</dt><dd>${escapeHtml(task.workspace_path)}</dd></div></dl>
      </div>
    </details>`;
    const row = `<div class="map-collapsible-row map-collapsible-context-row${current ? " is-current" : ""}${selected ? " is-selected" : ""}" role="none">
      ${hasChildren ? renderToggle("context", nodeId, expanded, task.title) : '<span class="map-collapsible-toggle-spacer" aria-hidden="true"></span>'}
      <button type="button" class="map-collapsible-item map-collapsible-context-item status-${escapeHtml(status.tone)}" role="treeitem" aria-level="${level}"${hasChildren ? ` aria-expanded="${expanded}"` : ""}${current ? ' aria-current="page"' : ""}${options.selectionMode ? ` aria-pressed="${selected}"` : ""} tabindex="${key === focusKey ? "0" : "-1"}" data-tree-key="${escapeHtml(key)}" data-tree-kind="context" data-tree-parent-key="${escapeHtml(parentKey)}" data-tree-has-children="${hasChildren}" data-action="${action}" data-task-id="${escapeHtml(nodeId)}">
        ${selectedMark}<span class="map-collapsible-title">${escapeHtml(task.title)}</span>${currentMark}${projectionMark}<span class="map-collapsible-status"><span class="map-collapsible-status-dot" aria-hidden="true"></span>${escapeHtml(status.label)}</span>
      </button>${sourceDetails}${actions}
    </div>`;
    if (!expanded) return row;
    const groupHtml = children.map(childId => renderNode(group, childId, level + 1, key, options, focusKey, seen)).join("");
    return `${row}<div class="map-collapsible-children" role="group">${groupHtml}</div>`;
  }

  function renderRoot(group, parentKey, options, focusKey) {
    if (!group.activeCount) return "";
    const rootNodeVisible = group.nodes.has(group.id);
    const meta = rootNodeVisible ? "" : `<div class="map-collapsible-root-meta" role="presentation"><strong>${escapeHtml(group.title)}</strong><span>根 Context 已${group.lifecycle === "archived" ? "归档" : "删除"} · ${group.activeCount} 个派生</span></div>`;
    const seen = new Set();
    const nodes = group.topIds.map(nodeId => renderNode(group, nodeId, 2, parentKey, options, focusKey, seen)).join("");
    return `<section class="map-collapsible-root" role="none">${meta}${nodes}</section>`;
  }

  function renderWorkspace(workspace, options, focusKey) {
    const expanded = workspace.activeCount > 0 && options.expandedWorkspaceIds.has(workspace.id);
    const key = itemKey("workspace", workspace.id);
    const roots = expanded ? workspace.roots.map(group => renderRoot(group, key, options, focusKey)).join("") : "";
    return `<section class="map-collapsible-workspace" role="none" data-workspace-id="${escapeHtml(workspace.id)}">
      <div class="map-collapsible-row map-collapsible-workspace-row" role="none">
        ${workspace.activeCount ? renderToggle("workspace", workspace.id, expanded, workspace.name) : '<span class="map-collapsible-toggle-spacer" aria-hidden="true"></span>'}
        <button type="button" class="map-collapsible-item map-collapsible-workspace-item" role="treeitem" aria-level="1"${workspace.activeCount ? ` aria-expanded="${expanded}"` : ""} tabindex="${key === focusKey ? "0" : "-1"}" data-tree-key="${escapeHtml(key)}" data-tree-kind="workspace" data-tree-parent-key="" data-tree-has-children="${workspace.activeCount > 0}" data-action="toggle-map-workspace" data-workspace-id="${escapeHtml(workspace.id)}">
          <span class="ui-icon icon-folder" aria-hidden="true"></span><span class="map-collapsible-title">${escapeHtml(workspace.name)}</span><span class="map-collapsible-count">${workspace.activeCount} 个 Context</span>
        </button>
      </div>
      ${expanded ? `<div class="map-collapsible-workspace-children" role="group">${roots}</div>` : ""}
    </section>`;
  }

  function render(model, rawOptions = {}) {
    if (!model?.workspaces?.length) return '<section class="map-collapsible-empty"><strong>暂无工作区任务</strong><span>创建任务后会在这里显示。</span></section>';
    const options = {
      activeTaskId: rawOptions.activeTaskId || null,
      expandedWorkspaceIds: rawOptions.expandedWorkspaceIds instanceof Set ? rawOptions.expandedWorkspaceIds : new Set(rawOptions.expandedWorkspaceIds || []),
      expandedContextIds: rawOptions.expandedContextIds instanceof Set ? rawOptions.expandedContextIds : new Set(rawOptions.expandedContextIds || []),
      selectedContextIds: rawOptions.selectedContextIds instanceof Set ? rawOptions.selectedContextIds : new Set(rawOptions.selectedContextIds || []),
      selectionMode: Boolean(rawOptions.selectionMode),
      focusKey: rawOptions.focusKey || "",
      presentStatus: typeof rawOptions.presentStatus === "function" ? rawOptions.presentStatus : status => ({ label: status, tone: "neutral" }),
      tasksById: new Map((rawOptions.tasks || []).map(task => [task.task_id, task])),
    };
    const focusKey = defaultFocusKey(model, options);
    return `<section class="map-collapsible-view"><div class="map-collapsible-tree" role="tree" aria-label="工作区与 Context">${model.workspaces.map(workspace => renderWorkspace(workspace, options, focusKey)).join("")}</div></section>`;
  }

  function treeKeyAction(key, currentIndex, items) {
    if (!Array.isArray(items) || currentIndex < 0 || currentIndex >= items.length) return null;
    const current = items[currentIndex];
    if (key === "ArrowDown") return { type: "focus", index: Math.min(currentIndex + 1, items.length - 1) };
    if (key === "ArrowUp") return { type: "focus", index: Math.max(currentIndex - 1, 0) };
    if (key === "Home") return { type: "focus", index: 0 };
    if (key === "End") return { type: "focus", index: items.length - 1 };
    if (key === "ArrowRight") {
      if (current.hasChildren && !current.expanded) return { type: "expand", key: current.key };
      const childIndex = items.findIndex((item, index) => index > currentIndex && item.parentKey === current.key);
      return childIndex >= 0 ? { type: "focus", index: childIndex } : null;
    }
    if (key === "ArrowLeft") {
      if (current.hasChildren && current.expanded) return { type: "collapse", key: current.key };
      const parentIndex = items.findIndex(item => item.key === current.parentKey);
      return parentIndex >= 0 ? { type: "focus", index: parentIndex } : null;
    }
    return null;
  }

  return { buildModel, expansionForTask, render, treeKeyAction };
});
