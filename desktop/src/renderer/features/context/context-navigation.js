/* Read-only navigation for current Context families and automatically curated work lines. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextNavigation = api;
})(globalThis, function () {
  "use strict";
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

  return Object.freeze({ orderTasksByTree, contextFamilyTasks });
});
