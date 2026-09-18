/*
 * 本文件对外提供会话渲染预算检查。输入为按真实 Loop 会话形态合成的长会话 fixture、流式增量序列、
 * 可注入的帧调度器与 app.js 的真实渲染函数；输出为五类断言结果：会话 HTML 体量与"体量不与工具输出
 * 同增"（Stage 1）、无原始载荷面与按需详情（Stage 1）、单元记忆化与增量应用（Stage 2）、流式已闭合块
 * 不重渲染（Stage 2）、渲染合并与后处理作用域（Stage 2）。工作流只构造数据并调用既有渲染函数，
 * 不修改运行时代码。示例：`node desktop/conversation-render-budget.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const test = require("node:test");
const path = require("node:path");
const { createAppHarness, readAppSource } = require(path.resolve(__dirname, "test-helper.cjs"));

const HTML_BUDGET_BYTES = 200 * 1024;
const PRE_BUDGET_BYTES = 40 * 1024;

function buildConversation({ toolCalls = 60, outputBytes = 30000 } = {}) {
  const output = "x".repeat(outputBytes);
  const messages = [
    { id: "m-0", role: "human", content: "开始" },
    { id: "m-1", role: "ai", content: "开场说明" },
  ];
  for (let i = 0; i < toolCalls; i += 1) {
    messages.push({
      id: `ai-${i}`,
      role: "ai",
      content: `第 ${i} 步说明`,
      reasoning_content: `第 ${i} 步推理：${"r".repeat(200)}`,
      tool_calls: [{ id: `call-${i}`, name: i % 2 ? "read_file" : "bash", args: { path: `/repo/file-${i}.go`, note: "n".repeat(120) } }],
    });
    messages.push({
      id: `tool-${i}`,
      role: "tool",
      tool_call_id: `call-${i}`,
      name: i % 2 ? "read_file" : "bash",
      status: "success",
      content: output,
    });
  }
  return messages;
}

function newHarness(options = {}) {
  const harness = createAppHarness({ fetch: true, ...options });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.__task = {
    task_id: "t-budget", thread_id: "th", workspace_id: "ws", title: "预算",
    harness_mode: "workspace", workspace_path: "C:\\ws", ui_state: {},
  };
  harness.vm.runInContext(
    "state.tasks=[__task]; state.activeTaskId=__task.task_id; state.details=new Map(); state.materialHistory=new Map(); state.streamBuffers=new Map();",
    harness.context,
  );
  return harness;
}

function renderConversation(harness, messages) {
  harness.context.__messages = messages;
  return harness.vm.runInContext("renderConversation({ messages: __messages }, __task)", harness.context);
}

function stats(harness) {
  return harness.vm.runInContext("conversationRenderStats()", harness.context);
}

function preBytes(html) {
  return [...html.matchAll(/<pre>([\s\S]*?)<\/pre>/g)].reduce((sum, m) => sum + m[1].length, 0);
}

test("长会话的会话 HTML 体量保持在预算内，且不随工具输出大小增长", () => {
  const harness = newHarness();
  const small = renderConversation(harness, buildConversation({ outputBytes: 10 * 1024 }));
  const large = renderConversation(harness, buildConversation({ outputBytes: 100 * 1024 }));

  const smallBytes = Buffer.byteLength(small, "utf8");
  const largeBytes = Buffer.byteLength(large, "utf8");
  assert.ok(smallBytes <= HTML_BUDGET_BYTES, `会话 HTML 应在 ${HTML_BUDGET_BYTES} 字节内，实际 ${smallBytes}`);
  assert.ok(preBytes(small) <= PRE_BUDGET_BYTES, `常驻 pre 文本应在 ${PRE_BUDGET_BYTES} 字节内，实际 ${preBytes(small)}`);
  assert.ok(
    largeBytes - smallBytes <= 4 * 1024,
    `工具输出增大 10 倍不应显著改变会话 HTML（${smallBytes} → ${largeBytes}）`,
  );
});

test("会话不再渲染原始载荷面（技术详情/参数 JSON/工具输出）", () => {
  const harness = newHarness();
  const html = renderConversation(harness, buildConversation({ toolCalls: 5, outputBytes: 20000 }));
  assert.ok(!html.includes("message-details"), "不得出现技术详情折叠块");
  assert.ok(!html.includes("技术详情"), "不得出现技术详情入口");
  assert.ok(!html.includes("tool_call_id"), "不得把工具调用身份写进 DOM");
  assert.ok(!html.includes("x".repeat(1000)), "工具输出正文不得常驻 DOM");
  assert.ok((html.match(/data-detail-lazy="1"/g) || []).length >= 10, "工具与推理行应给出按需详情占位");
});

test("工具行摘要保留可读信息，完整输出在展开时按需生成", () => {
  const harness = newHarness();
  const html = renderConversation(harness, buildConversation({ toolCalls: 5, outputBytes: 20000 }));
  assert.match(html, /<strong>read_file<\/strong>/, "工具名保留在摘要行");
  assert.match(html, /\/repo\/file-1\.go/, "参数摘要保留在摘要行");
  assert.match(html, /完成/, "状态保留在摘要行");

  const detail = harness.vm.runInContext(`
    (() => {
      const messages = ${JSON.stringify(buildConversation({ toolCalls: 5, outputBytes: 20000 }))};
      const event = FocusConversationEvents.normalize(messages).find(item => item.type === "tool");
      return FocusConversationEvents.renderEventDetail(event);
    })()
  `, harness.context);
  assert.match(detail, /参数/, "详情体在按需渲染时给出参数");
  assert.match(detail, /输出/, "详情体在按需渲染时给出完整输出");
  assert.ok(detail.includes("x".repeat(20000)), "详情体在按需渲染时给出完整输出正文");
});

test("尾部追加一条事件行只重建该行，容器内其余行命中缓存", () => {
  const harness = newHarness();
  const rows = size => {
    const messages = [{ id: "h-0", role: "human", content: "开始" }];
    for (let i = 0; i < size; i += 1) {
      messages.push({
        id: `ai-${i}`, role: "ai", content: "", reasoning_content: `Think ${i}`,
        tool_calls: [{ id: `c-${i}`, name: "read_file", args: { path: `f-${i}.txt` } }],
      });
      messages.push({ id: `t-${i}`, role: "tool", tool_call_id: `c-${i}`, name: "read_file", status: "success", content: `结果 ${i}` });
    }
    return messages;
  };
  const base = rows(12);
  const baseHtml = renderConversation(harness, base);
  const afterBase = stats(harness);
  assert.ok(afterBase.rowsBuilt >= 24, `基线应产出全部事件行（12 对 = 24 行），实际 ${afterBase.rowsBuilt}`);
  assert.equal(
    (baseHtml.match(/class="conversation-event-sequence"/g) || []).length,
    1,
    "无可见正文的助手消息应聚成同一个执行序列（真实载荷形态）",
  );

  // 追加一条只有未决工具调用的助手消息：只多出一行，且落在同一个序列内
  renderConversation(harness, [...base, {
    id: "ai-x", role: "ai", content: "", tool_calls: [{ id: "c-x", name: "read_file", args: { path: "x.txt" } }],
  }]);
  const afterAppend = stats(harness);
  assert.equal(afterAppend.rowsBuilt - afterBase.rowsBuilt, 1, "追加一行只应重建这一行");
  assert.ok(afterAppend.rowsReused - afterBase.rowsReused >= 24, "容器内其余行必须命中行级缓存");
});

test("长行容器上连续相同帧零行级渲染", () => {
  const harness = newHarness();
  const messages = [{ id: "h-0", role: "human", content: "开始" }];
  for (let i = 0; i < 12; i += 1) {
    messages.push({
      id: `ai-${i}`, role: "ai", content: "", reasoning_content: `Think ${i}`,
      tool_calls: [{ id: `c-${i}`, name: "read_file", args: { path: `f-${i}.txt` } }],
    });
    messages.push({ id: `t-${i}`, role: "tool", tool_call_id: `c-${i}`, name: "read_file", status: "success", content: `结果 ${i}` });
  }
  renderConversation(harness, messages);
  const afterFirst = stats(harness);
  assert.ok(afterFirst.rowsBuilt >= 24, `基线应产出 24 行事件，实际 ${afterFirst.rowsBuilt}`);

  for (let frame = 0; frame < 20; frame += 1) renderConversation(harness, messages);
  const afterTwenty = stats(harness);
  assert.equal(afterTwenty.rowsBuilt, afterFirst.rowsBuilt, "连续相同帧不得重新产出任何行");
  assert.equal(afterTwenty.built, afterFirst.built, "连续相同帧不得重建任何单元");
  // 相同帧由单元级缓存整体吸收（序列 HTML 命中），行级循环因此根本不被触及——比"逐行命中"更强。
  assert.ok(
    afterTwenty.reused - afterFirst.reused >= 20,
    `相同帧必须命中单元级缓存（复用增量 ${afterTwenty.reused - afterFirst.reused}）`,
  );
});

test("行级缓存有条目上限，不随会话历史增长", () => {
  const harness = newHarness();
  const messages = [{ id: "h-0", role: "human", content: "开始" }];
  for (let i = 0; i < 2600; i += 1) {
    messages.push({
      id: `ai-${i}`, role: "ai", content: "", reasoning_content: `Think ${i}`,
      tool_calls: [{ id: `c-${i}`, name: "read_file", args: { path: `f-${i}.txt` } }],
    });
    messages.push({ id: `t-${i}`, role: "tool", tool_call_id: `c-${i}`, name: "read_file", status: "success", content: `结果 ${i}` });
  }
  const rendered = harness.vm.runInContext(
    `(() => {
       const task = __task;
       const detail = { messages: ${JSON.stringify(messages)} };
       const input = _conversationRenderInput(detail, task);
       input.windowLimit = 100000;
       input.windowRows = 100000;
       const html = FocusConversationRender.renderConversation(input);
       return { html: html.length, stats: conversationRenderStats() };
     })()`,
    harness.context,
  );
  assert.ok(rendered.html > 1000000, "该用例应真的渲染出上千行");
  assert.ok(rendered.stats.cacheLimit > 0, "缓存上限必须可观测");
  assert.ok(
    rendered.stats.cached <= rendered.stats.cacheLimit,
    `缓存条目数应受上限约束，实际 ${rendered.stats.cached} > ${rendered.stats.cacheLimit}`,
  );
  assert.ok(
    rendered.stats.rowsBuilt > rendered.stats.cacheLimit,
    "本用例必须真的产出了超过上限的行，否则没有验证到淘汰",
  );
});

test("连续相同帧零重建，追加一条消息只重建一个单元", () => {
  const harness = newHarness();
  const base = [{ id: "h-1", role: "human", content: "开始" }, { id: "a-1", role: "ai", content: "第一段" }];
  renderConversation(harness, base);
  const afterFirst = stats(harness);

  for (let i = 0; i < 50; i += 1) renderConversation(harness, base);
  const afterFifty = stats(harness);
  assert.equal(afterFifty.built, afterFirst.built, "连续相同帧不得重建任何单元");
  assert.ok(afterFifty.reused >= 50 * afterFirst.built, "相同帧必须命中缓存");

  renderConversation(harness, [...base, { id: "h-2", role: "human", content: "继续" }]);
  const afterAppend = stats(harness);
  assert.equal(afterAppend.built - afterFifty.built, 1, "追加一条消息只应重建它自己的单元");
});

test("长会话按窗口挂载，加载更早内容提高窗口", () => {
  const harness = newHarness();
  const messages = [];
  for (let i = 0; i < 500; i += 1) messages.push({ id: `m-${i}`, role: "human", content: `第 ${i} 条消息` });
  const first = renderConversation(harness, messages);
  const units = (first.match(/data-message-key=/g) || []).length;
  assert.ok(units > 0, "窗口内必须有单元");
  assert.ok(units <= 90, `长会话的常驻单元数应有上限，实际 ${units}`);
  assert.match(first, /data-action="load-earlier-conversation"/, "应给出加载更早内容的入口");

  harness.vm.runInContext("conversationView.increaseWindow('t-budget')", harness.context);
  const second = renderConversation(harness, messages);
  const unitsAfter = (second.match(/data-message-key=/g) || []).length;
  assert.ok(unitsAfter > units, `提高窗口后应挂载更多单元（${units} → ${unitsAfter}）`);
});

test("流式追加只重建未闭合尾块，已闭合块复用", () => {
  const harness = newHarness();
  const buffer = { taskId: "t-budget", text: "", reasoning: "", messageId: "msg-stream", blocks: [], blockEntries: [] };
  harness.context.__buffer = buffer;
  const before = stats(harness);
  for (let frame = 0; frame < 60; frame += 1) {
    harness.vm.runInContext(
      `__buffer.text += "第 " + ${frame} + " 段正文内容，用来验证已闭合块不会被重渲染。\\n\\n"; renderStreamingContent(__buffer);`,
      harness.context,
    );
  }
  const after = stats(harness);
  const built = after.streamingBuilt - before.streamingBuilt;
  const reused = after.streamingReused - before.streamingReused;
  assert.ok(built <= 60 + 60, `已闭合块不得重渲染：块渲染次数 ${built} 应接近帧数`);
  assert.ok(reused >= 60, `闭合块必须命中复用：复用次数 ${reused}`);
});

test("同一帧内多次状态变化只触发一次渲染", () => {
  const frames = [];
  const harness = newHarness({ globals: { requestAnimationFrame: callback => frames.push(callback) } });
  let renders = 0;
  harness.vm.runInContext("render = () => { globalThis.__renderCount = (globalThis.__renderCount || 0) + 1; }", harness.context);
  harness.vm.runInContext("for (let i = 0; i < 5; i += 1) scheduleRender();", harness.context);
  renders = harness.vm.runInContext("globalThis.__renderCount || 0", harness.context);
  assert.equal(renders, 0, "合并期内不得立即渲染");
  assert.equal(frames.length, 1, "同一 tick 内只应排入一次帧回调");
  frames.forEach(callback => callback());
  renders = harness.vm.runInContext("globalThis.__renderCount || 0", harness.context);
  assert.equal(renders, 1, "帧回调只渲染一次");
});

test("会话增量更新不触发全量后处理遍历", () => {
  const harness = createAppHarness({ fetch: true });
  harness.vm.runInContext(
    "globalThis.__postRenderCalls = { i18n: 0, bind: 0 };"
    + " window.FocusI18n = { apply: () => { globalThis.__postRenderCalls.i18n += 1; }, locale: () => 'zh-CN', setLocale: () => false, t: key => key };"
    + " MaterialContentLoader.prototype.bindAll = function (scope) { globalThis.__postRenderCalls.bind += 1; globalThis.__postRenderCalls.bindScope = scope; };",
    harness.context,
  );
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.__task = {
    task_id: "t-budget", thread_id: "th", workspace_id: "ws", title: "预算",
    harness_mode: "workspace", workspace_path: "C:\\ws", ui_state: {},
  };
  harness.vm.runInContext(
    "state.tasks=[__task]; state.activeTaskId=__task.task_id; state.details=new Map(); state.materialHistory=new Map();"
    + " state.streamBuffers=new Map(); state.view='focus'; state.details.set(__task.task_id, { messages: [] });"
    + " globalThis.__before = { ...globalThis.__postRenderCalls };",
    harness.context,
  );
  harness.vm.runInContext("replaceConversation(__task, [{ id: 'm-1', role: 'ai', content: '增量' }])", harness.context);
  const before = harness.vm.runInContext("({ ...globalThis.__before })", harness.context);
  const after = harness.vm.runInContext("({ ...globalThis.__postRenderCalls })", harness.context);
  assert.equal(after.i18n - before.i18n, 0, "会话增量更新不得触发界面文案全量应用");
  assert.equal(after.bind - before.bind, 1, "会话增量更新只应做一次材料绑定");
  assert.equal(
    harness.vm.runInContext("globalThis.__postRenderCalls.bindScope === document.querySelector('#conversation')", harness.context),
    true,
    "材料绑定必须以会话子树为范围",
  );
  assert.equal(
    harness.vm.runInContext("globalThis.__postRenderCalls.bindScope === document.querySelector('#app')", harness.context),
    false,
    "材料绑定不得以整页为范围",
  );
});

test("同 id 消息内容变化只重建它自己", () => {
  const harness = newHarness();
  const base = [{ id: "h-1", role: "human", content: "开始" }, { id: "a-1", role: "ai", content: "第一段" }];
  renderConversation(harness, base);
  const before = stats(harness);
  renderConversation(harness, [{ id: "h-1", role: "human", content: "开始" }, { id: "a-1", role: "ai", content: "第一段续写" }]);
  const after = stats(harness);
  assert.equal(after.built - before.built, 1, "内容变化的单元应且只应重建一次");
  assert.ok(after.reused - before.reused >= 1, "未变化单元必须命中缓存");
});

test("加载更早内容提高窗口并按高度差保持阅读位置", () => {
  const node = {
    innerHTML: "", dataset: {}, attributes: [], children: [], childNodes: [], firstChild: null,
    scrollTop: 400, scrollHeight: 1000, clientHeight: 300,
    classList: { contains: () => false, toggle() {} },
    querySelector: () => null, querySelectorAll: () => [], matches: () => false,
    replaceChildren() {}, insertBefore() {}, append() {}, prepend() {}, remove() {}, replaceWith() {},
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
    hasAttribute: () => false, focus() {},
  };
  const harness = createAppHarness({ fetch: true, selectors: { "#conversation": node } });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.__task = {
    task_id: "t-anchor", thread_id: "th", workspace_id: "ws", title: "t",
    harness_mode: "workspace", workspace_path: "C:\\ws", ui_state: {},
  };
  harness.vm.runInContext(
    "state.tasks=[__task]; state.activeTaskId=__task.task_id; state.details=new Map(); state.materialHistory=new Map(); state.streamBuffers=new Map();",
    harness.context,
  );
  harness.context.__conv = node;
  harness.vm.runInContext("conversationView.loadEarlier(__conv, 't-anchor', () => { __conv.scrollHeight = 1500; })", harness.context);
  assert.equal(node.scrollTop, 900, "加载更早内容后阅读位置应按高度差下移");
  assert.equal(
    harness.vm.runInContext("conversationView.windowLimit('t-anchor')", harness.context),
    160,
    "窗口应提高一个步长",
  );
});

test("流式正文两条路径共用同一块缓存，详情按需只填充被展开的那一条", () => {
  const harness = newHarness();
  harness.context.__buffer = { taskId: "t-budget", text: "第一段\n\n第二段", reasoning: "", messageId: "m1", blocks: [], blockEntries: [] };
  harness.vm.runInContext("state.streamBuffers.set('run-x', __buffer)", harness.context);
  const before = stats(harness);
  renderConversation(harness, buildConversation({ toolCalls: 5, outputBytes: 2000 }));
  const afterRender = stats(harness);
  assert.ok(afterRender.streamingBuilt - before.streamingBuilt >= 2, "渲染时流式正文应产出块");

  harness.vm.runInContext("FocusConversationRender.streamingBlocks(mdRenderer, __buffer)", harness.context);
  const afterSecond = stats(harness);
  assert.equal(afterSecond.streamingBuilt - afterRender.streamingBuilt, 0, "同一缓冲第二次取块必须全部命中缓存");

  const captured = [];
  const fakeBody = () => ({ innerHTML: "", dataset: { detailLazy: "1" } });
  const fakeDetails = key => {
    const body = fakeBody();
    return {
      body,
      node: {
        dataset: { eventKey: key },
        open: true,
        matches: selector => selector.includes("details.conversation-event"),
        querySelector: selector => (selector.includes("data-detail-lazy") ? body : null),
      },
    };
  };
  harness.context.__host = { addEventListener: (type, handler) => captured.push(handler) };
  harness.vm.runInContext("conversationView.mountLazyDetails(__host)", harness.context);
  const first = fakeDetails("tool:call-1");
  const second = fakeDetails("tool:call-2");
  captured[0]({ target: first.node });
  assert.match(first.body.innerHTML, /参数/, "被展开的那一条获得详情");
  assert.equal(second.body.innerHTML, "", "未展开的条目不得被填充");
});
