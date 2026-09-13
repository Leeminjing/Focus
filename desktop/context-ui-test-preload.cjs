/*
 * 本文件为 Context 桌面交互回归页提供确定性的本地 API。
 * 输入为 app.js 发出的同源请求，输出为固定任务、lineage 和长消息快照；
 * 工作流不访问网络或数据库，仅让真实桌面页面复现滚动与树排序行为。
 */
const tasks = [
  { task_id: "child", title: "同名 Context", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace/这是一个用于验证最小窗口不会横向溢出的非常长目录名称/another-very-long-directory-name/focus", thread_id: "thread-child-with-a-long-identifier-that-must-remain-accessible", active_run: null },
  { task_id: "blocked", title: "同名 Context", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-blocked", active_run: null },
  { task_id: "root", title: "同名 Context", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-root", active_run: null },
  { task_id: "sibling", title: "安全 Context", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-sibling", active_run: null },
  { task_id: "merged", title: "合并 Context", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-merged", active_run: null },
  { task_id: "foreign-root", title: "其他线程", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-foreign-root", active_run: null },
  { task_id: "foreign-child", title: "其他线程派生", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-foreign-child", active_run: null },
];

const tree = [
  { context_id: "root", title: "同名 Context", depth: 0, projection_status: "root", editable: false, cache_hit_rate: null, cache_input_tokens: 0, cache_hit_tokens: 0, parents: [] },
  { context_id: "child", title: "同名 Context", depth: 1, projection_status: "valid", editable: true, cache_hit_rate: 0.25, cache_input_tokens: 200, cache_hit_tokens: 50, parents: [{ context_id: "root" }] },
  { context_id: "blocked", title: "同名 Context", depth: 1, projection_status: "approval_required", parents: [{ context_id: "root" }] },
  { context_id: "sibling", title: "安全 Context", depth: 1, projection_status: "valid", parents: [{ context_id: "root" }] },
  { context_id: "merged", title: "合并 Context", depth: 2, projection_status: "valid", parents: [{ context_id: "child" }, { context_id: "sibling" }] },
  { context_id: "foreign-root", title: "其他线程", depth: 0, projection_status: "root", parents: [] },
  { context_id: "foreign-child", title: "其他线程派生", depth: 1, projection_status: "valid", parents: [{ context_id: "foreign-root" }] },
];

Array.from({ length: 10 }, (_value, index) => {
  tasks.push({ task_id: `extra-${index}`, title: `扩展 Context ${index + 1}`, workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: `thread-extra-${index}`, active_run: null });
  tree.push({ context_id: `extra-${index}`, title: `扩展 Context ${index + 1}`, depth: 1, projection_status: "valid", parents: [{ context_id: "root" }] });
});

const snapshotMessages = Array.from({ length: 14 }, (_value, index) => ({
  ...(index === 12 ? {} : { id: index === 13 ? "message-11" : `message-${index}` }),
  role: index % 2 ? "ai" : "human",
  content: `消息 ${index}\n${"long context content ".repeat(18)}`,
}));
snapshotMessages[0] = {
  id: "message-0",
  role: "tool",
  content: "工具结果\n" + "long context content ".repeat(18),
  tool_call_id: "call-source",
  name: "search",
  additional_kwargs: { source: { exact: true } },
};

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

const uiStates = new Map();
const conversationMessages = taskId => Array.from({ length: 12 }, (_value, index) => ({
  id: `${taskId}-message-${index}`,
  role: index % 2 ? "ai" : "human",
  content: `${taskId} 独立消息 ${index}\n${"conversation content ".repeat(10)}`,
}));

window.focusDesktop = { runtime: () => ({ apiBase: "http://focus.test", session: "test-session" }) };
window.fetch = async (input, options = {}) => {
  const path = new URL(String(input), "http://focus.test").pathname;
  if (path === "/desktop/api/bootstrap") return json({ tasks, equipment: { models: [], tools: [], skills: [], permissions: [] } });
  if (path === "/desktop/api/tasks") return json(tasks);
  if (path === "/desktop/api/workspaces/workspace/contexts/tree") return json(tree);
  if (/\/desktop\/api\/tasks\/[^/]+\/ui-state$/.test(path)) {
    const taskId = path.split("/").at(-2);
    uiStates.set(taskId, JSON.parse(options.body || "{}"));
    return json(uiStates.get(taskId));
  }
  if (/\/desktop\/api\/tasks\/[^/]+$/.test(path)) {
    const taskId = path.split("/").at(-1);
    const context = taskId === "child"
      ? { context_id: taskId, projection_status: "valid", editable: true, authored_messages: [{ role: "human", content: "尚未运行，可继续编辑" }], sources: [{ context_id: "root", checkpoint_id: "checkpoint" }], repair_manifest: [], issues: [] }
      : taskId === "blocked"
        ? { context_id: taskId, projection_status: "approval_required", authored_messages: [{ role: "unknown", content: "保留" }], sources: [{ context_id: "root", checkpoint_id: "checkpoint" }], repair_manifest: [], issues: [{ reason: "测试非法角色", original: { role: "unknown", content: "保留" }, proposed: { role: "human", content: "保留" } }] }
        : null;
    return json({ task_id: taskId, messages: conversationMessages(taskId), ui_state: uiStates.get(taskId) || {}, active_run: null, context });
  }
  if (/\/desktop\/api\/tasks\/[^/]+\/(materials|agents)$/.test(path)) return json([]);
  if (/\/desktop\/api\/plugin\/spatial-patrol\/tasks\/[^/]+\/anchors$/.test(path)) return json([]);
  if (path === "/desktop/api/plugin/spatial-patrol/metadata") return json({ page_count: 1 });
  if (path === "/desktop/api/plugin/spatial-patrol/text") return json({ content: "# 文件工作台\n\n这是一份用于视觉验收的 Markdown 材料。\n\n- 支持字符锚点\n- 保持重排和滚动坐标\n- 权限只在 DOCX 操作时显式选择\n\n## 说明\n\n文件工作台与任务记录并存，不会替换当前对话。" });
  if (/\/desktop\/api\/tasks\/[^/]+\/skills$/.test(path)) return json({ skills: [] });
  if (/\/desktop\/api\/contexts\/[^/]+\/snapshot$/.test(path)) return json({ checkpoint_id: "checkpoint", messages: snapshotMessages });
  return json({ detail: `Unhandled test route: ${path}` }, 404);
};
