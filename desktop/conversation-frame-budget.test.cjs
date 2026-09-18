/*
 * 本文件对外提供会话区"每帧工作量"的预算检查。输入为按真实载荷形态合成的会话（只有工具调用与推理、
 * 没有可见正文）、可注入布局属性的会话容器节点与 app.js 的真实写入路径；输出为七类断言结果：
 * 滚动策略只按阅读意图与高度差决策（三情形）、一帧内布局读取与滚动写入各不超过一次、未贴底帧写入后
 * 不再读布局、常驻 DOM 按行数受限且不切断工具调用组、窗口滑动不整体重建执行序列且阅读位置不变、
 * 可见正文以 `STREAM_TEXT_LIMIT` 为界（越限不进入可见 DOM 且该帧不渲染正文）、会话容器的锚点策略显式声明。
 * 工作流只构造数据并调用既有函数，不修改运行时代码。示例：`node desktop/conversation-frame-budget.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { createAppHarness, readAppSource } = require(path.resolve(__dirname, "test-helper.cjs"));

const DOM_PROTOTYPE = Object.getPrototypeOf(require(path.resolve(__dirname, "test-dom.cjs")).createElement("div"));
const WINDOW_ROWS = 90;

function buildEventOnlyFlow(cycles = 12) {
  const messages = [{ id: "h-0", role: "human", content: "开始" }];
  for (let index = 0; index < cycles; index += 1) {
    messages.push({
      id: `ai-${index}`,
      role: "ai",
      content: "",
      reasoning_content: `Think ${index}`,
      tool_calls: [{ id: `call-${index}`, name: "read_file", args: { path: `f-${index}.txt` } }],
    });
    messages.push({
      id: `tool-${index}`,
      role: "tool",
      tool_call_id: `call-${index}`,
      name: "read_file",
      status: "success",
      content: `结果 ${index}`,
    });
  }
  return messages;
}

function newHarness() {
  const harness = createAppHarness({ fetch: true });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`
    globalThis.__task = {
      task_id: "t-frame", thread_id: "th", workspace_id: "ws", title: "帧预算",
      harness_mode: "workspace", workspace_path: "C:\\\\ws", ui_state: {},
    };
    state.view = "focus";
    state.tasks = [__task];
    state.activeTaskId = __task.task_id;
    state.details = new Map();
    state.materialHistory = new Map();
    state.materialGroups = new Map();
    state.materials = new Map();
    state.agents = new Map();
    state.streams = new Map();
    state.streamBuffers = new Map();
    state.streamFrames = new Map();
    state.commitment.tracePanel = null;
    state.commitment.reviewPanel = null;
    state.accessReviews = { panel: null, payload: null, taskId: null, key: null, busy: false };
    document.querySelector("#conversation").replaceChildren();
  `, harness.context);
  return harness;
}

function renderFresh(harness, messages) {
  harness.context.__messages = messages;
  harness.vm.runInContext(
    "document.querySelector('#conversation').replaceChildren(); replaceConversation(__task, __messages);",
    harness.context,
  );
}

function applySnapshot(harness, messages, runId) {
  harness.context.__messages = messages;
  harness.context.__runId = runId;
  harness.vm.runInContext("replaceConversation(__task, __messages, __runId)", harness.context);
}

test("滚动策略：底部且增长才跟随，净高度减少与不在底部都不动", () => {
  const harness = newHarness();

  // 每个情形用一个全新的会话容器，基线互不污染：先按写入前的高度登记，再模拟写入后的高度
  const frame = (top, before, after, client = 300) => harness.vm.runInContext(`(() => {
    const conversation = document.createElement("div");
    conversation.scrollHeight = ${before};
    conversation.clientHeight = ${client};
    conversation.scrollTop = ${top};
    conversationView.trackScroll(conversation);
    conversation.scrollHeight = ${after};
    const delta = conversationView.syncScrollAfterWrite(conversation);
    return { delta, top: conversation.scrollTop };
  })()`, harness.context);

  // 情形 1：位于底部且内容增长 → 跟随，且位移等于高度增量
  const grown = frame(700, 1000, 1100);
  assert.equal(grown.delta, 100, "底部增长时应按高度差回填");
  assert.equal(grown.top, 1100, "跟随后的位置应是新的底部");

  // 情形 2：位于底部但净高度减少（临时节点被回收）→ 本模块不得发起滚动写入
  const shrunk = frame(700, 1000, 930);
  assert.equal(shrunk.delta, 0, "净高度减少时不得由策略发起位移");
  assert.equal(shrunk.top, 700, "净高度减少时策略不得改写滚动位置");

  // 情形 3：不在底部（用户向上阅读）→ 一律不动
  const reading = frame(100, 1000, 1400);
  assert.equal(reading.delta, 0, "不在底部时不得跟随");
  assert.equal(reading.top, 100, "不在底部时内容变化不得改变视口位置");
});

test("一帧内布局读取与滚动写入各不超过一次", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(6);
  renderFresh(harness, flow);
  const counted = harness.vm.runInContext(`
    (() => {
      const conversation = document.querySelector("#conversation");
      let height = conversation.scrollHeight;
      let top = conversation.scrollTop;
      globalThis.__layout = { reads: 0, writes: 0 };
      Object.defineProperty(conversation, "scrollHeight", {
        configurable: true,
        get() { globalThis.__layout.reads += 1; return height; },
        set(value) { height = value; },
      });
      Object.defineProperty(conversation, "scrollTop", {
        configurable: true,
        get() { return top; },
        set(value) { globalThis.__layout.writes += 1; top = value; },
      });
      return true;
    })()
  `, harness.context);
  assert.equal(counted, true);

  applySnapshot(harness, [...flow, {
    id: "ai-x", role: "ai", content: "", tool_calls: [{ id: "call-x", name: "read_file", args: {} }],
  }]);
  const layout = harness.vm.runInContext("({ ...globalThis.__layout })", harness.context);
  assert.ok(layout.reads <= 1, `一帧内布局读取应不超过一次，实际 ${layout.reads}`);
  assert.ok(layout.writes <= 1, `一帧内滚动写入应不超过一次，实际 ${layout.writes}`);
});

test("未贴底的一帧：写入后不再读布局，写入至多一次", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(120);
  harness.context.__flow = flow;
  harness.context.__log = [];
  const renderWindow = limit => harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const input = _conversationRenderInput({ messages: __flow }, __task);
    input.windowLimit = ${limit};
    conversationView.reconcile(conv, FocusConversationRender.renderConversation(input));
    return conv.querySelectorAll(".conversation-event[data-event-key]").length;
  })()`, harness.context);

  const mounted = renderWindow(80);
  assert.ok(mounted > 20, `基线应挂载足够多的行，实际 ${mounted}`);
  const log = harness.context.__log;
  const originals = {
    insertBefore: DOM_PROTOTYPE.insertBefore,
    remove: DOM_PROTOTYPE.remove,
    replaceChildren: DOM_PROTOTYPE.replaceChildren,
  };
  DOM_PROTOTYPE.insertBefore = function (...args) { log.push("mutate"); return originals.insertBefore.apply(this, args); };
  DOM_PROTOTYPE.remove = function (...args) { log.push("mutate"); return originals.remove.apply(this, args); };
  DOM_PROTOTYPE.replaceChildren = function (...args) { log.push("mutate"); return originals.replaceChildren.apply(this, args); };
  let result;
  try {
    result = harness.vm.runInContext(`(() => {
      const conv = document.querySelector("#conversation");
      const rows = () => [...conv.querySelectorAll(".conversation-event[data-event-key]")];
      conv.clientHeight = 300;
      conv.scrollHeight = rows().length * 30;
      rows().forEach(row => {
        row.getBoundingClientRect = () => {
          __log.push("layout");
          const index = rows().indexOf(row);   // 按当前 DOM 位置即时求值，等同真实布局
          const top = index * 30 - (conv.scrollTop || 0);
          return { top, bottom: top + 30, height: 30 };
        };
      });
      conv.getBoundingClientRect = () => ({ top: 0, bottom: 300, height: 300 });
      conv.scrollTop = 60;
      conversationView.trackScroll(conv);   // 登记基线（真实路径由 replaceConversation 在写入前调用）
      conv.dispatchEvent({ type: "scroll" });   // 用户向上阅读

      let height = conv.scrollHeight;
      let top = conv.scrollTop;
      Object.defineProperty(conv, "scrollHeight", {
        configurable: true,
        get() { __log.push("layout"); return height; },
        set(value) { height = value; },
      });
      Object.defineProperty(conv, "scrollTop", {
        configurable: true,
        get() { return top; },
        set(value) { __log.push("write"); top = value; },
      });
      __log.length = 0;

      // 窗口上边界前移两行（不贴底 ⇒ 需要按阅读锚点回正）
      const input = _conversationRenderInput({ messages: __flow }, __task);
      input.windowLimit = 78;
      conversationView.reconcile(conv, FocusConversationRender.renderConversation(input));
      conversationView.syncScrollAfterWrite(conv);

      const trace = [...__log];
      const lastWrite = trace.lastIndexOf("write");
      const lastMutate = trace.lastIndexOf("mutate");
      return {
        writes: trace.filter(entry => entry === "write").length,
        readsAfterMutation: lastMutate === -1 ? 0 : trace.slice(lastMutate).filter(entry => entry === "layout").length,
        readsAfterWrite: lastWrite === -1 ? 0 : trace.slice(lastWrite).filter(entry => entry === "layout").length,
        mutated: lastMutate !== -1,
        scrollTop: conv.scrollTop,
      };
    })()`, harness.context);
  } finally {
    DOM_PROTOTYPE.insertBefore = originals.insertBefore;
    DOM_PROTOTYPE.remove = originals.remove;
    DOM_PROTOTYPE.replaceChildren = originals.replaceChildren;
  }
  assert.equal(result.mutated, true, "该用例必须真的改动了 DOM");
  assert.equal(result.writes, 1, `未贴底时应恰好回正一次滚动位置，实际 ${result.writes}`);
  assert.equal(result.scrollTop, 0, "回正量应等于被淘汰行的真实位移（60 → 0）");
  assert.ok(
    result.readsAfterMutation <= 2,
    `写入后只允许一次高度读取与一次锚点校核，实际 ${result.readsAfterMutation}`,
  );
  assert.equal(
    result.readsAfterWrite, 0,
    "写入滚动位置之后不得再读布局（否则就是读—写—读的交替）",
  );
});

test("常驻 DOM 按行数受限，且窗口起点不切断工具调用组", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(200);
  renderFresh(harness, flow);
  const mounted = harness.vm.runInContext(`(() => {
    const conversation = document.querySelector("#conversation");
    return {
      rows: conversation.querySelectorAll(".conversation-event[data-event-key]").length,
      units: conversation.children.length,
      earlier: conversation.querySelectorAll("[data-action='load-earlier-conversation']").length,
    };
  })()`, harness.context);
  assert.ok(mounted.rows > 0, "必须挂载了事件行");
  assert.ok(
    mounted.rows <= WINDOW_ROWS + 10,
    `常驻行数应受行数上限约束（≤ ${WINDOW_ROWS} + 边界回退），实际 ${mounted.rows}`,
  );
  assert.ok(mounted.rows < 300, "真实工作量必须真的被裁掉，而不是把上百行挤进常驻 DOM");
  assert.equal(mounted.earlier, 1, "被裁掉的部分必须提供加载更早入口");

  // 窗口起点必须落在完整的工具调用组上：工具结果不得与其调用消息分离，
  // 否则用户会在窗口首行看到"没有来源的工具结果"——长工具会话里上百行的形态下尤其明显。
  harness.context.__flow = flow;
  const start = harness.vm.runInContext(
    "FocusConversationRender.windowStartIndex(__flow, 80, 90)", harness.context,
  );
  assert.ok(start > 0, "长会话必须真的被裁剪");
  const callIds = new Set();
  let orphans = 0;
  for (const message of flow.slice(start)) {
    if (message.role === "tool") {
      if (!callIds.has(message.tool_call_id)) orphans += 1;
      continue;
    }
    for (const call of (message.tool_calls || [])) if (call?.id) callIds.add(call.id);
  }
  assert.equal(orphans, 0, "窗口内不得出现没有调用来源的工具结果");
});

test("窗口上边界前移时，向上阅读的位置落在同一行", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(120);
  harness.context.__flow = flow;

  // 行高 30 的可预测布局盒：行顶 = 行序 * 30 - scrollTop，容器视口高 300
  const paint = () => harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const rows = [...conv.querySelectorAll(".conversation-event[data-event-key]")];
    conv.clientHeight = 300;
    conv.scrollHeight = rows.length * 30;
    conv.getBoundingClientRect = () => ({ top: 0, bottom: 300, height: 300 });
    rows.forEach((row, index) => {
      row.getBoundingClientRect = () => {
        const top = index * 30 - (conv.scrollTop || 0);
        return { top, bottom: top + 30, height: 30 };
      };
    });
    return rows.length;
  })()`, harness.context);

  const renderWindow = limit => harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const input = _conversationRenderInput({ messages: __flow }, __task);
    input.windowLimit = ${limit};
    conversationView.reconcile(conv, FocusConversationRender.renderConversation(input));
    return conv.querySelectorAll(".conversation-event[data-event-key]").length;
  })()`, harness.context);

  const onMount = renderWindow(80);
  const conv = harness.document.querySelector("#conversation");
  conv.scrollTop = 900;
  paint();
  harness.vm.runInContext(
    "conversationView.trackScroll(document.querySelector('#conversation'))", harness.context,
  );
  const key = harness.vm.runInContext(`(() => {
    const rows = [...document.querySelector("#conversation").querySelectorAll(".conversation-event[data-event-key]")];
    return rows[Math.floor(rows.length / 2)].dataset.eventKey;
  })()`, harness.context);
  const probe = () => harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const row = conv.querySelector('.conversation-event[data-event-key="' + ${JSON.stringify(key)} + '"]');
    return { top: row ? Math.round(row.getBoundingClientRect().top) : null, scrollTop: conv.scrollTop, rows: conv.querySelectorAll(".conversation-event[data-event-key]").length };
  })()`, harness.context);
  const before = probe();
  assert.ok(before.top !== null, "必须能读到正在阅读的事件行");
  harness.context.__seqBefore = harness.vm.runInContext(
    "document.querySelector('#conversation').querySelector('.conversation-event-sequence')", harness.context,
  );

  // 同一尾部、上边界前移两行：窗口滑动淘汰视口上方的行
  const afterMount = renderWindow(78);
  const removedRows = onMount - afterMount;
  paint();
  harness.vm.runInContext(
    "conversationView.syncScrollAfterWrite(document.querySelector('#conversation'))", harness.context,
  );
  const after = probe();
  const sameSequence = harness.vm.runInContext(
    "document.querySelector('#conversation').querySelector('.conversation-event-sequence') === __seqBefore",
    harness.context,
  );
  assert.ok(removedRows > 0, `该用例必须真的淘汰了上方行，实际 ${onMount} → ${afterMount}`);
  assert.ok(after.top !== null, "阅读中的行不应被窗口淘汰");
  assert.equal(
    after.top, before.top,
    `窗口滑动后阅读位置必须落在同一行（${before.top} → ${after.top}）`,
  );
  assert.equal(
    before.scrollTop - after.scrollTop, removedRows * 30,
    "补偿量必须等于视口上方被淘汰行的高度",
  );
  assert.equal(
    sameSequence, true,
    "窗口滑动 MUST NOT 整体重建执行序列（身份不取首行键）",
  );
});

test("越限的流式正文不进入可见 DOM，且该帧不解析正文", () => {
  const harness = newHarness();
  renderFresh(harness, [{ id: "h-1", role: "human", content: "开始" }]);
  harness.context.__big = "文件正文行内容\n".repeat(10000);
  const measured = harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const before = conversationRenderStats();
    const buffer = { taskId: __task.task_id, text: __big, reasoning: "", messageId: "m-big", blocks: [], blockEntries: [] };
    state.streamBuffers.set("run-big", buffer);
    conversationView.syncStreamingPlaceholder(conv, "run-big", buffer);
    const after = conversationRenderStats();
    const placeholder = conv.querySelector("[data-stream-run]");
    return {
      limit: FocusConversationRender.STREAM_TEXT_LIMIT,
      textBytes: __big.length,
      visibleBody: Boolean(placeholder && placeholder.querySelector(".message-rich")),
      leaksIntoConversation: conv.textContent.includes(__big.slice(0, 64)),
      badge: placeholder ? (placeholder.querySelector(".ui-badge") || {}).textContent : null,
      streamingBuilt: after.streamingBuilt - before.streamingBuilt,
      streamingReused: after.streamingReused - before.streamingReused,
    };
  })()`, harness.context);
  assert.ok(measured.textBytes > measured.limit, `该用例必须真的越限（${measured.textBytes} > ${measured.limit}）`);
  assert.equal(measured.visibleBody, false, "越限正文不得进入可见 DOM");
  assert.equal(measured.leaksIntoConversation, false, "越限正文不得出现在会话文本里");
  assert.equal(measured.badge, "生成中", "占位指示仍在，只是不呈现正文");
  assert.equal(
    measured.streamingBuilt + measured.streamingReused, 0,
    "越限正文不得交给 markdown 渲染（该帧不产生任何块级渲染工作）",
  );
});

test("流式累积以可见正文上限为界，上限内照常渲染且落定后按需可展开", () => {
  const harness = newHarness();
  renderFresh(harness, [{ id: "h-1", role: "human", content: "开始" }]);
  const result = harness.vm.runInContext(`(() => {
    const conv = document.querySelector("#conversation");
    const envelope = (content, messageId) => ({
      thread_id: __task.thread_id, workspace_id: __task.workspace_id,
      agent_id: "main:" + __task.task_id, run_id: "run-a", data: { content, message_id: messageId },
    });
    appendToken(envelope("第 1 段正文\\n\\n第 2 段正文\\n\\n", "m-a"));
    const buffer = state.streamBuffers.get("run-a");
    conversationView.syncStreamingPlaceholder(conv, "run-a", buffer);
    const under = {
      textBytes: buffer.text.length,
      visibleBody: Boolean(conv.querySelector("[data-stream-run] .message-rich")),
      shown: conv.querySelector("[data-stream-run] .message-rich").textContent.includes("第 1 段正文"),
    };

    // 真实形态：工具消息 id 变化使缓冲重置，随后一段超长正文到达
    const limit = FocusConversationRender.STREAM_TEXT_LIMIT;
    appendToken(envelope("超长正文".repeat(Math.ceil(limit / 4) + 1000), "m-tool"));
    const afterBig = state.streamBuffers.get("run-a").text.length;
    appendToken(envelope("越限之后仍不断到达的增量", "m-tool"));
    const afterMore = state.streamBuffers.get("run-a").text.length;
    conversationView.syncStreamingPlaceholder(conv, "run-a", state.streamBuffers.get("run-a"));
    const over = {
      grown: afterBig > limit,
      stoppedGrowing: afterMore === afterBig,
      visibleBody: Boolean(conv.querySelector("[data-stream-run] .message-rich")),
    };

    const blob = __big;
    replaceConversation(__task, [
      { id: "h-1", role: "human", content: "开始" },
      { id: "ai-1", role: "ai", content: "", reasoning_content: "准备读取", tool_calls: [{ id: "c1", name: "read_file", args: { path: "f.txt" } }] },
      { id: "m-tool", role: "tool", tool_call_id: "c1", name: "read_file", status: "success", content: blob },
    ]);
    const row = conv.querySelector(".conversation-event.is-tool");
    row.open = true;
    row.dispatchEvent({ type: "toggle" });
    const settled = {
      placeholders: conv.querySelectorAll("[data-stream-run]").length,
      toolRows: conv.querySelectorAll(".conversation-event.is-tool").length,
      summary: row.querySelector("summary").textContent,
      detailHasFullOutput: row.querySelector(".conversation-event-detail").textContent.includes(blob.slice(0, 40)),
      detailBytes: row.querySelector(".conversation-event-detail").textContent.length,
    };
    return { under, over, settled };
  })()`, (() => { harness.context.__big = "工具输出正文行\n".repeat(3000); return harness.context; })());
  assert.equal(result.under.visibleBody, true, "上限内的正文必须照常渲染");
  assert.equal(result.under.shown, true, "上限内的正文内容必须真的进入可见 DOM");
  assert.equal(result.over.grown, true, "越限后累积正文必须确实超过上限");
  assert.equal(result.over.stoppedGrowing, true, "越限后不再并入后续增量（累积有界）");
  assert.equal(result.over.visibleBody, false, "越限后可见正文必须退出 DOM");
  assert.equal(result.settled.placeholders, 0, "快照落定后占位必须回收");
  assert.equal(result.settled.toolRows, 1, "工具结果按正式形态呈现为一行工具行");
  assert.equal(result.settled.detailHasFullOutput, true, "完整输出仍可按需展开得到");
});

test("会话容器显式声明锚点策略，且不扩散到全局", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "styles", "views.css"), "utf8");
  const rule = css.match(/\.conversation\s*\{([^}]*)\}/);
  assert.ok(rule, "必须存在 .conversation 规则");
  assert.match(rule[1], /overflow-anchor:\s*none/, "会话容器必须显式退出浏览器滚动锚定");
  assert.doesNotMatch(css, /\*\s*\{[^}]*overflow-anchor/, "锚点策略不得扩散为全局声明");
});
