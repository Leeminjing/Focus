/*
 * 本文件对外提供「会话视图一致性」的桌面回归检查。输入为 app.js 的真实渲染/对账函数、真实
 * 会话消息形态（已发布 revision、含工具与推理的执行流、流式增量）与受控的同源 API 响应，
 * 输出为四类断言结果：会话条目形状不丢失、唯一写者与唯一渲染入口、迟到刷新不覆盖会话、
 * 流式占位按消息身份换段并在快照到达后收敛。工作流只加载既有模块并在 VM 宿主内调用既有函数，
 * 不修改任何运行时代码。示例：`node desktop/conversation-live-consistency.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const test = require("node:test");
const path = require("node:path");
const { createAppHarness, readAppSource } = require(path.resolve(__dirname, "test-helper.cjs"));

const TASK_ID = "task-live-1";
const THREAD_ID = "thread-live-1";
const WORKSPACE_ID = "ws-live-1";

const PUBLISHED = [
  { id: "m-1", role: "human", content: "你好" },
  { id: "m-2", role: "ai", content: "问候" },
];

const LIVE = [
  ...PUBLISHED,
  { id: "m-3", role: "human", content: "【任务目标】修复" },
  {
    id: "m-4",
    role: "ai",
    content: "我先勘察工作区",
    reasoning_content: "Let me explore the workspace",
    tool_calls: [{ id: "call-1", name: "list_files", args: { path: "." } }],
  },
  { id: "m-5", role: "tool", tool_call_id: "call-1", name: "list_files", status: "success", content: "a.go\nb.go" },
];

function newHarness() {
  const harness = createAppHarness({ fetch: true });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.__task = {
    task_id: TASK_ID,
    thread_id: THREAD_ID,
    workspace_id: WORKSPACE_ID,
    title: "任务",
    harness_mode: "workspace",
    workspace_path: "C:\\ws",
    ui_state: {},
  };
  harness.vm.runInContext(
    "state.tasks = [__task]; state.activeTaskId = __task.task_id; state.details = new Map(); state.materialHistory = new Map(); state.streamBuffers = new Map();",
    harness.context,
  );
  return harness;
}

function renderConversation(harness, messages) {
  harness.context.__messages = messages;
  return harness.vm.runInContext(
    "renderConversation({ messages: __messages }, __task)",
    harness.context,
  );
}

function countRows(html) {
  const matches = pattern => (html.match(pattern) || []).length;
  return {
    messages: matches(/class="work-record message/g),
    tool: matches(/class="conversation-event is-tool/g),
    reasoning: matches(/class="conversation-event is-reasoning"/g),
  };
}

test("执行流形态完整：工具与推理条目在对话区渲染，已发布前缀不产生虚构条目", () => {
  const harness = newHarness();
  const live = countRows(renderConversation(harness, LIVE));
  assert.strictEqual(live.tool, 1);
  assert.strictEqual(live.reasoning, 1);
  assert.ok(live.messages >= 3);

  const published = countRows(renderConversation(harness, PUBLISHED));
  assert.strictEqual(published.tool, 0);
  assert.strictEqual(published.reasoning, 0);
});

test("同一份消息的渲染结果与调用路径无关，且会话区只由对账写入", () => {
  const harness = newHarness();
  assert.strictEqual(renderConversation(harness, LIVE), renderConversation(harness, LIVE));

  const source = readAppSource();
  assert.strictEqual(source.split("renderConversation(").length - 1, 4, "会话渲染应只有一个定义与三处调用（初始渲染、运行流更新、加载更早内容）");
  assert.strictEqual(source.split('<div class="conversation" id="conversation"></div>').length - 1, 1, "顶层骨架必须留空会话容器");
  assert.strictEqual(source.split("reconcileConversationMarkup(").length - 1, 3, "会话内容只经对账写入");
});

test("迟到或跨 Context 的刷新响应不得覆盖更新的会话状态", async () => {
  const harness = newHarness();
  const { context } = harness;
  let taskDetailCalls = 0;
  let releaseSlow = null;
  const payloadFor = (pathname, tag) => {
    if (!pathname.endsWith(`/tasks/${TASK_ID}`)) return [];
    return { task_id: TASK_ID, messages: [{ id: `m-${tag}`, role: "ai", content: tag }] };
  };
  context.fetch = url => {
    const pathname = String(url).replace(/^.*\/desktop\/api/, "");
    if (pathname.endsWith(`/tasks/${TASK_ID}`)) {
      taskDetailCalls += 1;
      const tag = taskDetailCalls === 1 ? "slow" : "fast";
      const json = async () => payloadFor(pathname, tag);
      if (tag === "slow") {
        return new Promise(resolve => {
          releaseSlow = () => resolve({ ok: true, status: 200, json });
        });
      }
      return Promise.resolve({ ok: true, status: 200, json });
    }
    const json = async () => payloadFor(pathname, "list");
    return Promise.resolve({ ok: true, status: 200, json });
  };

  const slow = context.hydrateActive(TASK_ID);
  const fast = context.hydrateActive(TASK_ID);
  await fast;
  context.__taskId = TASK_ID;
  assert.strictEqual(
    harness.vm.runInContext("state.details.get(__taskId).messages[0].id", context),
    "m-fast",
  );
  releaseSlow();
  await slow;
  assert.strictEqual(
    harness.vm.runInContext("state.details.get(__taskId).messages[0].id", context),
    "m-fast",
    "迟到的旧响应必须被丢弃",
  );
});

test("流式占位按消息身份换段，并在消息进入权威列表后收敛", () => {
  const harness = newHarness();
  const { context } = harness;
  const envelope = (messageId, content) => ({
    thread_id: THREAD_ID,
    workspace_id: WORKSPACE_ID,
    agent_id: `main:${TASK_ID}`,
    run_id: "run-1",
    data: { message_id: messageId, content },
  });
  const bufferText = () => harness.vm.runInContext("state.streamBuffers.get('run-1').text", context);

  context.appendStreamDelta(envelope("msg-1", "第一段"), "text");
  context.appendStreamDelta(envelope("msg-1", "续写"), "text");
  assert.strictEqual(bufferText(), "第一段续写");

  context.appendStreamDelta(envelope("msg-2", "第二段"), "text");
  assert.strictEqual(bufferText(), "第二段", "同一 Run 内换消息必须换段");

  context.finalizeStreamingOnSnapshot(TASK_ID, [{ id: "msg-2" }]);
  assert.strictEqual(
    harness.vm.runInContext("state.streamBuffers.has('run-1')", context),
    false,
    "快照确认后占位必须收敛",
  );
});

test("SSE 未收到 end 就断开时从运行 API 对账终态并刷新会话", async () => {
  let source = null;
  class TestEventSource {
    constructor() {
      source = this;
      this.listeners = new Map();
      this.closed = false;
    }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    emit(type, event = {}) { this.listeners.get(type)?.(event); }
    close() { this.closed = true; }
  }
  const harness = createAppHarness({ fetch: true, globals: { EventSource: TestEventSource } });
  const { context } = harness;
  context.EventSource = TestEventSource;
  harness.vm.runInContext(readAppSource(), context);
  context.__task = {
    task_id: TASK_ID,
    thread_id: THREAD_ID,
    workspace_id: WORKSPACE_ID,
    title: "任务",
  };
  context.__run = {
    run_id: "run-disconnected",
    task_id: TASK_ID,
    thread_id: THREAD_ID,
    kind: "main",
    status: "running",
  };
  const refreshed = [];
  context.api = async path => {
    assert.strictEqual(path, "/desktop/api/runs/run-disconnected");
    return { ...context.__run, status: "success" };
  };
  context.refreshTaskAfterTxn = async taskId => { refreshed.push(taskId); };
  context.scheduleRender = () => {};
  harness.vm.runInContext(
    "state.tasks = [__task]; state.activeTaskId = __task.task_id; state.details.set(__task.task_id, { active_run: __run, messages: [] }); listenToRun(__run);",
    context,
  );

  source.emit("error");
  await new Promise(resolve => setImmediate(resolve));

  assert.deepStrictEqual(refreshed, [TASK_ID]);
  assert.strictEqual(source.closed, true);
  assert.strictEqual(harness.vm.runInContext("state.streams.has(__run.run_id)", context), false);
});

test("切换 Context 后返回的旧响应只落它自己的缓存，当前会话状态不被改写", async () => {
  const harness = newHarness();
  const { context } = harness;
  let releaseSlow = null;
  context.fetch = url => {
    const pathname = String(url).replace(/^.*\/desktop\/api/, "");
    const taskId = pathname.includes("task-a") ? "task-a" : "task-b";
    const json = async () =>
      pathname.endsWith(`/tasks/${taskId}`)
        ? { task_id: taskId, messages: [{ id: `m-${taskId}`, role: "ai", content: taskId }] }
        : [];
    if (pathname.endsWith("/tasks/task-a")) {
      return new Promise(resolve => {
        releaseSlow = () => resolve({ ok: true, status: 200, json });
      });
    }
    return Promise.resolve({ ok: true, status: 200, json });
  };
  context.__tasks = [
    { task_id: "task-a", thread_id: "th-a", workspace_id: "ws-a", title: "A" },
    { task_id: "task-b", thread_id: "th-b", workspace_id: "ws-b", title: "B" },
  ];
  harness.vm.runInContext("state.tasks = __tasks; state.activeTaskId = 'task-a';", context);

  const pendingA = context.hydrateActive("task-a");
  harness.vm.runInContext("state.activeTaskId = 'task-b';", context);
  await context.hydrateActive("task-b");
  const currentId = () => harness.vm.runInContext("state.details.get('task-b').messages[0].id", context);
  assert.strictEqual(currentId(), "m-task-b");

  releaseSlow();
  await pendingA;
  assert.strictEqual(currentId(), "m-task-b", "当前 Context 的会话状态不得被旧响应改写");
  assert.strictEqual(
    harness.vm.runInContext("state.details.get('task-a').messages[0].id", context),
    "m-task-a",
    "旧响应只落它自己的缓存",
  );
});

test("顶层重建接管既有会话节点，展开态与滚动位置保持", () => {
  const spyConversation = {
    innerHTML: "",
    dataset: {},
    attributes: [],
    children: [],
    childNodes: [],
    firstChild: null,
    scrollTop: 0,
    scrollHeight: 1000,
    clientHeight: 100,
    replacedWith: null,
    classList: { contains: () => false, toggle() {} },
    querySelector: () => null,
    querySelectorAll: () => [],
    matches: () => false,
    replaceWith(node) { this.replacedWith = node; },
    replaceChildren() {},
    insertBefore() {},
    append() {},
    prepend() {},
    remove() {},
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    hasAttribute: () => false,
    focus() {},
  };
  const harness = createAppHarness({ fetch: true, selectors: { "#conversation": spyConversation } });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.__task = {
    task_id: TASK_ID,
    thread_id: THREAD_ID,
    workspace_id: WORKSPACE_ID,
    title: "任务",
    harness_mode: "workspace",
    workspace_path: "C:\\ws",
    ui_state: {},
  };
  harness.vm.runInContext(
    "state.tasks = [__task]; state.activeTaskId = __task.task_id; state.view = 'focus';"
      + " state.details = new Map([[__task.task_id, { messages: [], ui_state: {} }]]);",
    harness.context,
  );

  harness.vm.runInContext("renderFocus()", harness.context);
  spyConversation.scrollTop = 120;
  harness.vm.runInContext("renderFocus()", harness.context);

  assert.strictEqual(spyConversation.replacedWith, spyConversation, "顶层重建必须接管既有会话节点");
  assert.strictEqual(spyConversation.scrollTop, 120, "接管后滚动位置保持");
});
