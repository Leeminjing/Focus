/*
 * 本文件验证宿主稳定性边界。输入为真实 app.js VM 与可控异步 API，输出为单次错误 body 解析和
 * task-scoped hydrate 断言；工作流确保纯文本错误不被覆盖、旧任务响应不污染当前任务。
 */
"use strict";

const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

async function testApiErrorBody() {
  const harness = createAppHarness({ globals: { Response } });
  harness.vm.runInContext(readAppSource(), harness.context);

  harness.context.fetch = async () => new Response("plain gateway failure", {
    status: 503,
    statusText: "Service Unavailable",
  });
  await assert.rejects(
    Promise.resolve(harness.context.api("/broken")),
    error => error.message === "plain gateway failure" && error.status === 503,
    "纯文本错误必须保留真实 body，不能二次读取后变成 Body unusable",
  );

  harness.context.fetch = async () => new Response("", { status: 500, statusText: "Internal Server Error" });
  await assert.rejects(
    Promise.resolve(harness.context.api("/empty")),
    error => error.message === "HTTP 500 Internal Server Error",
    "空错误 body 必须回退到 HTTP 状态",
  );
}

async function testTaskScopedHydration() {
  const harness = createAppHarness({ globals: { setTimeout } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    api = async path => {
      const taskId = path.includes('task-a') ? 'task-a' : 'task-b';
      await new Promise(resolve => setTimeout(resolve, taskId === 'task-a' ? 30 : 2));
      if (path.endsWith('/materials')) return [{ material_id: 'material-' + taskId }];
      if (path.endsWith('/agents')) return [{ agent_id: 'agent-' + taskId }];
      if (path.endsWith('/skills')) return { skills: [{ name: 'skill-' + taskId }] };
      return { task_id: taskId, messages: [], active_run: null };
    };
    state.tasks = [
      { task_id: 'task-a', workspace_id: 'workspace', thread_id: 'thread-a', title: 'A' },
      { task_id: 'task-b', workspace_id: 'workspace', thread_id: 'thread-b', title: 'B' },
    ];
    state.activeTaskId = 'task-a';
    const first = hydrateActive('task-a');
    state.activeTaskId = 'task-b';
    const second = hydrateActive('task-b');
    await Promise.all([first, second]);
    return {
      detailA: state.details.get('task-a')?.task_id,
      detailB: state.details.get('task-b')?.task_id,
      materialA: state.materials.get('task-a')?.[0]?.material_id,
      materialB: state.materials.get('task-b')?.[0]?.material_id,
    };
  })()`, harness.context);

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    detailA: "task-a",
    detailB: "task-b",
    materialA: "material-task-a",
    materialB: "material-task-b",
  });
}

async function testStaleWorkspaceRequestsDoNotNavigate() {
  const harness = createAppHarness({ globals: { setTimeout } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    state.tasks = [
      { task_id: 'task-a', workspace_id: 'workspace', thread_id: 'thread-a', title: 'A' },
      { task_id: 'task-b', workspace_id: 'workspace', thread_id: 'thread-b', title: 'B' },
    ];
    state.activeTaskId = 'task-a';
    state.view = 'focus';
    render = () => {};
    setStatus = () => {};
    api = async path => {
      await new Promise(resolve => setTimeout(resolve, 20));
      if (path.endsWith('/messages')) return { messages: [{ id: 'm1', role: 'human', content: 'A' }] };
      if (path.includes('/snapshot')) return { checkpoint_id: 'cp-a', messages: [] };
      return { task_id: 'task-a', pending_compression: { usage: 90, limit: 100 } };
    };
    const compression = openCompressionView(state.tasks[0], { usage: 90, limit: 100 });
    taskSwitchSequence += 1;
    state.activeTaskId = 'task-b';
    await compression;
    const afterCompression = { active: state.activeTaskId, view: state.view, task: state.compression.taskId };

    taskSwitchSequence -= 1;
    state.activeTaskId = 'task-a';
    const context = openContextEditor('task-a');
    cancelPendingViewRequests();
    state.view = 'plugins';
    await context;
    return { afterCompression, afterContext: { active: state.activeTaskId, view: state.view, draft: state.contextDraft } };
  })()`, harness.context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    afterCompression: { active: "task-b", view: "focus", task: null },
    afterContext: { active: "task-a", view: "plugins", draft: null },
  });
}

async function testLatestDraftAndPluginNavigationWins() {
  const harness = createAppHarness({ globals: { setTimeout } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    state.tasks = [
      { task_id: 'task-a', workspace_id: 'workspace', thread_id: 'thread-a', title: 'A' },
      { task_id: 'task-b', workspace_id: 'workspace', thread_id: 'thread-b', title: 'B' },
    ];
    render = () => {};
    setStatus = () => {};
    persistFocusState = async () => {};
    api = async path => {
      const isA = path.includes('task-a');
      await new Promise(resolve => setTimeout(resolve, isA ? 25 : path.includes('/plugins') ? 20 : 2));
      if (path.endsWith('/skills')) return { skills: [{ name: isA ? 'a' : 'b' }] };
      if (path.endsWith('/drafts/open')) return { draft_id: isA ? 'draft-a' : 'draft-b', task_id: isA ? 'task-a' : 'task-b' };
      if (path.endsWith('/traces')) return { traces: [] };
      return { plugins: [], interfaces: {} };
    };
    const first = openDraft('task-a');
    const second = openDraft('task-b');
    await Promise.all([first, second]);
    const draft = { active: state.activeTaskId, view: state.view, hasA: state.drafts.has('task-a'), hasB: state.drafts.has('task-b') };

    state.view = 'focus';
    const plugin = openPluginsView();
    await new Promise(resolve => setTimeout(resolve, 2));
    cancelPendingViewRequests();
    state.view = 'map';
    await plugin;
    return { draft, pluginView: state.view };
  })()`, harness.context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    draft: { active: "task-b", view: "draft", hasA: false, hasB: true },
    pluginView: "map",
  });
}

async function testLatestDraftSaveWins() {
  const harness = createAppHarness({ globals: { setTimeout } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    const draft = {
      draft_id: 'draft-a', task_id: 'task-a', system_prompt: 'old', history_messages: [],
      final_human_message: 'run', equipment: { permissions: [], skills: [] }, token_estimate: 1,
    };
    state.activeTaskId = 'task-a';
    state.view = 'draft';
    state.drafts.set('task-a', draft);
    setStatus = () => {};
    api = async (_path, options) => {
      const payload = JSON.parse(options.body);
      await new Promise(resolve => setTimeout(resolve, payload.system_prompt === 'old' ? 25 : 2));
      return { ...draft, ...payload, token_estimate: payload.system_prompt === 'old' ? 10 : 20 };
    };
    const oldSave = saveDraft('task-a', { syncDom: false, revision: 1, cancelTimer: false });
    draft.system_prompt = 'new';
    const newSave = saveDraft('task-a', { syncDom: false, revision: 2, cancelTimer: false });
    const outcomes = await Promise.all([oldSave, newSave]);
    const saved = state.drafts.get('task-a');
    api = async () => { throw new Error('disk full'); };
    const failed = await saveDraft('task-a', { syncDom: false, revision: 3, cancelTimer: false });
    return { outcomes, prompt: saved.system_prompt, tokens: saved.token_estimate, failed };
  })()`, harness.context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    outcomes: [true, true], prompt: "new", tokens: 20, failed: false,
  });
}

async function testAttachmentCommitBoundary() {
  const mainInput = { value: "带图消息", disabled: false };
  const harness = createAppHarness({
    selectors: { "#mainInput": mainInput, "#composerFeedback": { className: "", textContent: "" } },
    globals: { crypto: webcrypto, TextEncoder },
  });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    state.tasks = [{ task_id: 'task-a', workspace_id: 'workspace', thread_id: 'thread-a', title: 'A' }];
    state.activeTaskId = 'task-a';
    state.details.set('task-a', { messages: [], ui_state: { input: '带图消息', skills: [] }, active_run: null });
    state.contextTrees.set('workspace', []);
    state.materials.set('task-a', [{ material_id: 'm1', relative_path: '.focus/attachments/a.png', is_image: true, size_bytes: 10 }]);
    state.mustView.set('task-a', ['m1']);
    renderFocus = () => {};
    persistFocusState = () => {};
    listenToRun = () => {};
    api = async () => { throw new Error('network down'); };
    await sendMain();
    const afterFailure = state.mustView.get('task-a').length;
    let apiCalls = 0;
    api = async () => { apiCalls += 1; return { run_id: 'run-ok', task_id: 'task-a', kind: 'main', status: 'pending' }; };
    await Promise.all([sendMain(), sendMain()]);
    return { afterFailure, afterSuccess: state.mustView.get('task-a').length, apiCalls };
  })()`, harness.context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { afterFailure: 1, afterSuccess: 0, apiCalls: 1 });
}

async function testPluginAssetsAreIdempotent() {
  const harness = createAppHarness();
  const appended = [];
  harness.document.createElement = tagName => ({
    tagName: String(tagName).toUpperCase(),
    remove() {},
  });
  harness.document.head = {
    append(node) {
      appended.push(`${node.tagName}:${node.src || node.href}`);
      if (node.tagName === "SCRIPT") node.onload();
    },
  };
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = await harness.vm.runInContext(`(async () => {
    const plugins = [{ name: 'demo plugin', status: 'active', desktop_assets: ['style.css', 'viewer.js', 'entry.js'] }];
    await hydratePluginAssets(plugins);
    await hydratePluginAssets(plugins);
    return true;
  })()`, harness.context);
  assert.equal(result, true);
  assert.deepEqual(appended, [
    "LINK:/plugins/demo%20plugin/desktop/style.css",
    "SCRIPT:/plugins/demo%20plugin/desktop/viewer.js",
    "SCRIPT:/plugins/demo%20plugin/desktop/entry.js",
  ]);
}

async function testConversationEventsAndKeyboardSubmit() {
  const input = {
    id: "mainInput",
    value: "发送一次",
    dataset: { skillInput: "main" },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    closest(selector) { return selector === "[data-skill-input]" ? this : null; },
  };
  const menu = { hidden: true };
  const harness = createAppHarness({ selectors: {
    '[data-skill-input="main"]': input,
    "#mainSkillList": menu,
  } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const rendered = harness.vm.runInContext(`(() => {
    const detail = { messages: [
      { role: 'human', content: '问题' },
      { role: 'ai', content: '回答', reasoning_content: '先分析' },
    ] };
    state.streamBuffers.set('run-stream', { taskId: 'task-a', text: '流式回答', reasoning: '流式思考' });
    return renderConversation(detail, { task_id: 'task-a', workspace_path: 'C:/workspace' });
  })()`, harness.context);
  assert.doesNotMatch(rendered, />YOU</);
  assert.doesNotMatch(rendered, />FOCUS</);
  assert.doesNotMatch(rendered, />你的指令</);
  assert.doesNotMatch(rendered, />助手</);
  assert.match(rendered, /Think/);
  assert.match(rendered, /生成中/);

  const lifecycleRendered = harness.vm.runInContext(`(() => {
    const early = 'EARLY-REASONING';
    const completeReasoning = early + '-' + 'x'.repeat(130) + '-LATEST-LIVE';
    const historical = renderConversation({ messages: [
      { role: 'ai', content: 'done', reasoning_content: completeReasoning },
    ] }, { task_id: 'history-task', workspace_path: 'C:/workspace' });
    const initial = renderStreamingContent({ text: '', reasoning: early });
    const streaming = renderStreamingContent({
      text: '',
      reasoning: completeReasoning,
    });
    const summary = html => (html.match(/<summary>([\\s\\S]*?)<\\/summary>/) || [])[1] || '';
    return { initial: summary(initial), historical: summary(historical), streaming: summary(streaming) };
  })()`, harness.context);
  assert.match(lifecycleRendered.initial, /EARLY-REASONING/, "首个 reasoning 增量立即可见");
  assert.doesNotMatch(lifecycleRendered.initial, /LATEST-LIVE/, "首个增量不包含尚未到达的内容");
  assert.match(lifecycleRendered.streaming, /LATEST-LIVE/, "活动 reasoning 展示最新进展");
  assert.doesNotMatch(lifecycleRendered.streaming, /EARLY-REASONING/, "活动 reasoning 不固定显示开头");
  assert.match(lifecycleRendered.historical, /EARLY-REASONING/, "历史 reasoning 保持稳定的开头摘要");
  assert.doesNotMatch(lifecycleRendered.historical, /LATEST-LIVE/, "历史 reasoning 不继续使用活动流尾部窗口");

  const managedRendered = harness.vm.runInContext(`renderConversation({
    context: { managed_status: 'following' },
    messages: [
      { role: 'system', content: '执行边界' },
      { role: 'human', content: '最终约束' },
      { role: 'ai', content: '已确认结论' },
    ],
  }, { task_id: 'task-a', workspace_path: 'C:/workspace' })`, harness.context);
  assert.match(managedRendered, /work-record-kicker">Human</);
  assert.match(managedRendered, /work-record-kicker">AI</);

  for (const role of ["System", "Human", "AI"]) {
    assert.equal(
      (managedRendered.match(new RegExp(`>${role}<\\/span>`, "g")) || []).length,
      1,
      `${role} 角色标签只渲染一次`,
    );
  }

  harness.vm.runInContext("globalThis.__sendCount = 0; sendMain = () => { __sendCount += 1; return Promise.resolve(); };", harness.context);
  const keydown = harness.listeners.get("keydown").at(-1);
  const event = overrides => ({
    key: "Enter", keyCode: 13, isComposing: false,
    shiftKey: false, altKey: false, ctrlKey: false, metaKey: false,
    target: input, prevented: false,
    preventDefault() { this.prevented = true; },
    ...overrides,
  });
  const plain = event({}); keydown(plain);
  const shifted = event({ shiftKey: true }); keydown(shifted);
  const composing = event({ isComposing: true }); keydown(composing);
  const ime = event({ keyCode: 229 }); keydown(ime);
  assert.equal(harness.context.__sendCount, 1);
  assert.equal(plain.prevented, true);
  assert.equal(shifted.prevented || false, false);
  assert.equal(composing.prevented || false, false);
  assert.equal(ime.prevented || false, false);

  input.value = "/";
  const picker = event({}); keydown(picker);
  assert.equal(harness.context.__sendCount, 1, "技能菜单 Enter 只能选择，不能发送");
  assert.equal(picker.prevented, true);
}

async function testActiveTaskSelectionInvariant() {
  const harness = createAppHarness();
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = harness.vm.runInContext(`(() => {
    state.activeTaskId = 'missing-task';
    replaceTasks([
      { task_id: 'archived-task', lifecycle: 'archived' },
      { task_id: 'active-task', lifecycle: 'active' },
    ]);
    const recovered = state.activeTaskId;

    replaceTasks([{ task_id: 'archived-only', lifecycle: 'archived' }]);
    const withoutActive = state.activeTaskId;
    return { recovered, withoutActive };
  })()`, harness.context);

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    recovered: "active-task",
    withoutActive: null,
  });
}

async function testRenderWithoutActiveTask() {
  const appNode = { dataset: {}, innerHTML: "", replaceChildren() {} };
  const harness = createAppHarness({ selectors: { "#app": appNode } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const result = harness.vm.runInContext(`(() => {
    state.tasks = [{ task_id: 'archived-task', lifecycle: 'archived' }];
    state.activeTaskId = null;
    state.view = 'focus';
    renderShellChrome = () => {};
    renderFocus = () => { throw new Error('task renderer must not run'); };
    render();
    return app.innerHTML;
  })()`, harness.context);

  assert.match(result, /暂无活动 Context/);
}

async function testUiActionErrorBoundary() {
  const harness = createAppHarness({ statusNode: "record" });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`runUiAction(async () => { throw new Error('selection drift'); });`, harness.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(harness.statusState.text, "页面操作失败: selection drift");
  assert.equal(harness.statusState.danger, true);
}

Promise.resolve()
  .then(testApiErrorBody)
  .then(testTaskScopedHydration)
  .then(testStaleWorkspaceRequestsDoNotNavigate)
  .then(testLatestDraftAndPluginNavigationWins)
  .then(testLatestDraftSaveWins)
  .then(testAttachmentCommitBoundary)
  .then(testPluginAssetsAreIdempotent)
  .then(testConversationEventsAndKeyboardSubmit)
  .then(testActiveTaskSelectionInvariant)
  .then(testRenderWithoutActiveTask)
  .then(testUiActionErrorBoundary)
  .then(() => console.log("app-stability: 错误解析、任务选择、异步边界、附件、紧凑会话与键盘提交边界通过"))
  .catch(error => { console.error(error); process.exitCode = 1; });
