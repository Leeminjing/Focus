/*
 * 本文件对外提供会话区 DOM 写入（对账）的正确性检查。输入为按真实增量快照形态合成的消息序列、具备真实
 * 子节点语义的最小 DOM 与 app.js 的真实写入路径；输出为十一类断言结果：增量写入不丢执行行、写入幂等且与
 * 写入路径无关、子节点归类闭合（无空序列滞留、规模与单元数一致）、非序列单元内容变化后不留旧节点、
 * 无身份键的裸节点被显式丢弃、声明保留的节点原位存活、流式占位随快照回收、展开态与按需详情体保持、
 * 计划阶段只读、测试脚手架提供真实子节点语义、容器直接写入只发生在声明允许的两类。工作流只构造数据并
 * 调用既有函数，不修改运行时代码。
 * 每个用例各自创建 VM 上下文，跨上下文比较前先取回宿主数组（数组原型不同，严格比较会误判）。
 * 示例：`node desktop/conversation-dom-reconciliation.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const test = require("node:test");
const path = require("node:path");
const { createAppHarness, readAppSource } = require(path.resolve(__dirname, "test-helper.cjs"));

const WINDOW_LIMIT = 80;

function buildExecutionFlow(cycles = 8) {
  const messages = [{ id: "h-0", role: "human", content: "开始" }];
  for (let index = 0; index < cycles; index += 1) {
    messages.push({
      id: `ai-${index}`,
      role: "ai",
      content: `第 ${index} 步`,
      reasoning_content: `第 ${index} 步推理`,
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

// 只有工具调用与推理、没有可见正文的会话（真实载荷形态：所有执行行落在同一个序列内）
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

const DOM_PROTOTYPE = Object.getPrototypeOf(require(path.resolve(__dirname, "test-dom.cjs")).createElement("div"));

// 记录一次调用期间真实发生的 DOM 写入（插入/整体替换/移除），用于断言"只触碰变化的部分"。
function trackDomWrites(run) {
  const ops = { insertBefore: [], replaceChildren: [], remove: [] };
  const originals = {
    insertBefore: DOM_PROTOTYPE.insertBefore,
    replaceChildren: DOM_PROTOTYPE.replaceChildren,
    remove: DOM_PROTOTYPE.remove,
  };
  DOM_PROTOTYPE.insertBefore = function (node, reference) {
    ops.insertBefore.push({ container: this, node, reference });
    return originals.insertBefore.call(this, node, reference);
  };
  DOM_PROTOTYPE.replaceChildren = function (...nodes) {
    ops.replaceChildren.push({ container: this, count: nodes.length });
    return originals.replaceChildren.apply(this, nodes);
  };
  DOM_PROTOTYPE.remove = function () {
    ops.remove.push({ node: this });
    return originals.remove.call(this);
  };
  try {
    run();
  } finally {
    DOM_PROTOTYPE.insertBefore = originals.insertBefore;
    DOM_PROTOTYPE.replaceChildren = originals.replaceChildren;
    DOM_PROTOTYPE.remove = originals.remove;
  }
  return ops;
}

function sequenceRows(harness) {
  return harness.vm.runInContext(
    "[...document.querySelector('#conversation').querySelectorAll('.conversation-event-sequence > .conversation-event')]",
    harness.context,
  );
}

function newHarness() {  const harness = createAppHarness({ fetch: true });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`
    globalThis.__task = {
      task_id: "t-dom", thread_id: "th", workspace_id: "ws", title: "对账",
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
    globalThis.__frames = [];
    globalThis.requestAnimationFrame = callback => { __frames.push(callback); return __frames.length; };
    document.querySelector("#conversation").replaceChildren();
  `, harness.context);
  return harness;
}

function applySnapshot(harness, messages, runId) {
  harness.context.__messages = messages;
  harness.context.__runId = runId;
  harness.vm.runInContext("replaceConversation(__task, __messages, __runId)", harness.context);
}

function renderFresh(harness, messages) {
  harness.context.__messages = messages;
  harness.vm.runInContext(
    "document.querySelector('#conversation').replaceChildren(); replaceConversation(__task, __messages);",
    harness.context,
  );
}

function snapshot(harness) {
  return harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const children = [...container.children];
    const keyOf = node => node.getAttribute("data-unit-key")
      || node.getAttribute("data-message-key")
      || node.getAttribute("data-divider-key")
      || node.getAttribute("data-stream-run")
      || node.getAttribute("class");
    const sequences = [...container.querySelectorAll(".conversation-event-sequence")];
    return {
      childCount: children.length,
      toolRows: container.querySelectorAll(".conversation-event.is-tool").length,
      reasoningRows: container.querySelectorAll(".conversation-event.is-reasoning").length,
      emptySequences: sequences.filter(node => !node.querySelector(".conversation-event[data-event-key]")).length,
      skeleton: children.map(node => node.tagName + "|" + keyOf(node)),
    };
  })()`, harness.context);
}

function nodeState(harness, selector) {
  harness.context.__selector = selector;
  return harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const node = container.querySelector(__selector);
    const detail = node && node.querySelector(".conversation-event-detail");
    return {
      present: Boolean(node),
      open: node ? node.open : null,
      detailLength: detail ? detail.innerHTML.length : 0,
      rows: container.querySelectorAll(__selector).length,
    };
  })()`, harness.context);
}

test("增量快照不丢失工具行与推理行", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(1);
  renderFresh(harness, flow.slice(0, 3));
  assert.equal(snapshot(harness).toolRows, 1, "基线：待定调用应呈现为一行工具调用");

  applySnapshot(harness, flow.slice(0, 4));
  assert.equal(snapshot(harness).toolRows, 1, "解析工具结果后工具行不得减少");

  renderFresh(harness, flow.slice(0, 4));
  assert.equal(snapshot(harness).toolRows, 1, "一次性写入同一状态应得到相同的工具行数");
});

test("写入幂等：同一状态写两次结果不变，且与写入路径无关", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(2);
  applySnapshot(harness, flow.slice(0, 3));
  applySnapshot(harness, flow.slice(0, 5));
  const once = snapshot(harness);

  applySnapshot(harness, flow.slice(0, 5));
  assert.deepStrictEqual(snapshot(harness), once, "同一状态重复写入必须得到同一 DOM");

  const reference = newHarness();
  renderFresh(reference, flow.slice(0, 5));
  const oneShot = snapshot(reference);
  assert.deepStrictEqual(
    [...once.skeleton],
    [...oneShot.skeleton],
    `逐步增量与一次性写入应得到同一子节点序列\n增量: ${JSON.stringify(once.skeleton)}\n一次性: ${JSON.stringify(oneShot.skeleton)}`,
  );
});

test("容器子节点归类闭合：无空序列滞留且规模与既有单元数一致", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(8);
  for (let size = 1; size <= flow.length; size += 1) applySnapshot(harness, flow.slice(0, size));
  const incremental = snapshot(harness);

  const reference = newHarness();
  renderFresh(reference, flow);
  const oneShot = snapshot(reference);

  assert.equal(incremental.emptySequences, 0, "空执行序列不得滞留在会话容器中");
  assert.equal(incremental.childCount, oneShot.childCount, "增量写入后的子节点数应与一次性写入一致");
  assert.equal(incremental.toolRows, oneShot.toolRows, "工具行数应与一次性写入一致");
  assert.equal(incremental.reasoningRows, oneShot.reasoningRows, "推理行数应与一次性写入一致");
});

test("声明保留的节点不被写入过程删除且位置不变", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(2);
  renderFresh(harness, flow.slice(0, 3));
  const placed = harness.vm.runInContext(`(() => {
    const panel = document.createElement("details");
    panel.className = "trace-panel is-live";
    panel.setAttribute("open", "");
    panel.innerHTML = "<summary>承诺轨迹</summary>";
    document.querySelector("#conversation").prepend(panel);
    globalThis.__panel = panel;
    return { first: document.querySelector("#conversation").firstElementChild === panel };
  })()`, harness.context);
  assert.equal(placed.first, true, "前置的保留节点应位于容器首位");

  applySnapshot(harness, flow.slice(0, 5));
  const kept = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    return {
      first: container.firstElementChild === __panel,
      count: container.querySelectorAll(".trace-panel").length,
      connected: __panel.parentNode === container,
    };
  })()`, harness.context);
  assert.equal(kept.connected, true, "保留节点不得被写入过程删除");
  assert.equal(kept.count, 1, "保留节点不得被重复插入");
  assert.equal(kept.first, true, "保留节点的位置不得被写入过程改变");
});

test("流式占位只在快照确实包含其正文时回收，中途快照不得回收", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(1);
  renderFresh(harness, flow.slice(0, 3));
  harness.vm.runInContext(`
    appendToken({
      thread_id: __task.thread_id, workspace_id: __task.workspace_id, agent_id: "main:" + __task.task_id,
      run_id: "run-z", data: { content: "正在分析文件", message_id: null },
    });
    __frames.forEach(frame => frame());
    __frames.length = 0;
  `, harness.context);
  const placeholders = () => harness.vm.runInContext(
    "document.querySelector('#conversation').querySelectorAll('[data-stream-run]').length", harness.context,
  );
  assert.equal(placeholders(), 1, "基线：流式追加应产生一个占位");

  // 1) 中途快照：正文尚未落定，占位必须留存（否则会先消失、再被下一条增量重建，流式正文只剩尾巴）
  applySnapshot(harness, flow.slice(0, 4), "run-z");
  assert.equal(placeholders(), 1, "中途快照不得回收占位");
  assert.deepStrictEqual(
    [...harness.vm.runInContext("state.streamBuffers.keys()", harness.context)],
    ["run-z"],
    "中途快照不得清掉流式缓冲",
  );

  // 2) 快照包含该缓冲的正文：占位被回收且正文只出现一次
  const finalized = [...flow.slice(0, 4), {
    id: "ai-fin", role: "ai", content: "正在分析文件，随后读取 forwarder.go。", tool_calls: [],
  }];
  applySnapshot(harness, finalized, "run-z");
  assert.equal(placeholders(), 0, "正文已落定的快照必须回收占位");
  assert.equal(harness.vm.runInContext("state.streamBuffers.size", harness.context), 0, "流式缓冲必须被清理");
  const occurrences = harness.vm.runInContext(
    "(document.querySelector('#conversation').textContent.match(/正在分析文件/g) || []).length",
    harness.context,
  );
  assert.equal(occurrences, 1, "落定后正文不得在会话里出现两次");
});

test("信封带消息 id 时按 id 命中回收占位", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(1);
  renderFresh(harness, flow.slice(0, 3));
  harness.vm.runInContext(`
    appendToken({
      thread_id: __task.thread_id, workspace_id: __task.workspace_id, agent_id: "main:" + __task.task_id,
      run_id: "run-id", data: { content: "另一次生成", message_id: "m-new" },
    });
    __frames.forEach(frame => frame());
    __frames.length = 0;
  `, harness.context);
  assert.equal(
    harness.vm.runInContext("document.querySelector('#conversation').querySelectorAll('[data-stream-run]').length", harness.context),
    1,
    "基线：产生了占位",
  );
  applySnapshot(harness, [...flow.slice(0, 4), { id: "m-new", role: "ai", content: "另一次生成", tool_calls: [] }], "run-id");
  assert.equal(
    harness.vm.runInContext("document.querySelector('#conversation').querySelectorAll('[data-stream-run]').length", harness.context),
    0,
    "快照含该消息 id 时应按 id 回收占位",
  );
});

test("尾部追加一行只插入该行，既有行不被重建", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(12);
  renderFresh(harness, flow);
  const before = [...sequenceRows(harness)];
  assert.ok(before.length >= 24, `基线应有 24 行事件，实际 ${before.length}`);

  const ops = trackDomWrites(() => {
    applySnapshot(harness, [...flow, {
      id: "ai-x", role: "ai", content: "",
      tool_calls: [{ id: "call-x", name: "read_file", args: { path: "x.txt" } }],
    }]);
  });

  const sequenceRebuilds = ops.replaceChildren.filter(op => String(op.container?.className || "").includes("conversation-event-sequence"));
  assert.equal(sequenceRebuilds.length, 0, "尾部追加不得整段重建执行序列");
  const existing = new Set(before);
  const reinserted = ops.insertBefore.filter(op => existing.has(op.node));
  assert.equal(reinserted.length, 0, "既有行不得被重新插入");
  const after = [...sequenceRows(harness)];
  assert.equal(after.length, before.length + 1, "只应新增一行");
  assert.ok(before.every(row => after.includes(row)), "既有行的节点身份必须保持");
});

test("单行内容变化只更新该行，其余行保持节点身份", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(12);
  renderFresh(harness, flow);
  const before = [...sequenceRows(harness)];
  assert.ok(before.length >= 24, `基线应有 24 行事件，实际 ${before.length}`);
  const targetKey = "reasoning:ai-5";
  const targetRow = before.find(row => row.dataset.eventKey === targetKey);
  assert.ok(targetRow, "夹具必须包含被改动的推理行");
  const summaryBefore = targetRow.querySelector("summary").outerHTML;

  // 改推理正文：摘要行（推理预览）随之变化，而工具输出在展开时才按需生成
  const changed = flow.map(message => (
    message.id === "ai-5" ? { ...message, reasoning_content: "Think 5（第二版，明显更长的推理）" } : message
  ));
  const ops = trackDomWrites(() => applySnapshot(harness, changed));

  const sequenceRebuilds = ops.replaceChildren.filter(op => String(op.container?.className || "").includes("conversation-event-sequence"));
  assert.equal(sequenceRebuilds.length, 0, "单行变化不得整段重建执行序列");
  const existing = new Set(before);
  const reinserted = ops.insertBefore.filter(op => existing.has(op.node));
  assert.equal(reinserted.length, 0, "未变化的行不得被重新插入");

  const after = [...sequenceRows(harness)];
  assert.equal(after.length, before.length, "单行内容变化不改变行数");
  assert.ok(
    before.filter(row => row.dataset.eventKey !== targetKey).every(row => after.includes(row)),
    "未变化的行必须保持节点身份",
  );
  const afterTarget = after.find(row => row.dataset.eventKey === targetKey);
  assert.equal(afterTarget, targetRow, "被改动的行应原地更新而不是换节点");
  assert.notEqual(afterTarget.querySelector("summary").outerHTML, summaryBefore, "被改动的行摘要必须更新");
});

test("状态不变的一帧不在会话容器内产生任何 DOM 写入", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(12);
  renderFresh(harness, flow);
  const ops = trackDomWrites(() => applySnapshot(harness, flow));
  // 模板解析会把目标 HTML 写进游离的 DocumentFragment，其子节点类名同样以 conversation- 开头；
  // 因此按"祖先链上是否出现会话容器节点本身"判断，而不是按类名。
  const conversation = harness.document.querySelector("#conversation");
  const inside = node => {
    let current = node;
    for (let depth = 0; current && depth < 64; depth += 1) {
      if (current === conversation) return true;
      if (current.parentNode === current) break;
      current = current.parentNode;
    }
    return false;
  };
  const inContainer = list => list.filter(op => inside(op.container || op.node));
  const describe = list => list.map(op => {
    const node = op.container || op.node;
    return `${node?.tagName || "?"}.${String(node?.className || "")}`;
  }).join(", ");
  assert.equal(inContainer(ops.replaceChildren).length, 0, `无变化帧不得整体替换：${describe(inContainer(ops.replaceChildren))}`);
  assert.equal(inContainer(ops.insertBefore).length, 0, `无变化帧不得插入或重排节点：${describe(inContainer(ops.insertBefore))}`);
  assert.equal(inContainer(ops.remove).length, 0, `无变化帧不得移除节点：${describe(inContainer(ops.remove))}`);
});

test("中间插入一行与窗口滑出时，只有涉及的行变化", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(6);
  const head = flow.slice(0, flow.length - 2);
  const tail = flow.slice(flow.length - 2);
  renderFresh(harness, [...head, ...tail]);
  const before = [...sequenceRows(harness)];
  const beforeKeys = before.map(row => row.dataset.eventKey);

  // 在中间插入一步（ai + tool 两行）
  const inserted = [
    { id: "ai-mid", role: "ai", content: "", reasoning_content: "Think mid", tool_calls: [{ id: "call-mid", name: "read_file", args: { path: "mid.txt" } }] },
    { id: "tool-mid", role: "tool", tool_call_id: "call-mid", name: "read_file", status: "success", content: "结果 mid" },
  ];
  const middle = [...head.slice(0, 2), ...inserted, ...head.slice(2), ...tail];
  applySnapshot(harness, middle);
  const after = [...sequenceRows(harness)];
  assert.equal(after.length, before.length + 2, "中间插入应新增两行");
  const afterKeys = after.map(row => row.dataset.eventKey);
  for (const key of beforeKeys) assert.ok(afterKeys.includes(key), `既有行 ${key} 不得消失`);
  const preserved = after.filter(row => before.includes(row)).length;
  assert.equal(preserved, before.length, "中间插入不得重建任何既有行");

  // 窗口滑出：把窗口压到最小，前部行被移除而其余行保持身份（用长会话触发）
  const long = newHarness();
  const many = buildEventOnlyFlow(60);
  renderFresh(long, many);
  const windowedBefore = [...sequenceRows(long)];
  applySnapshot(long, [...many, { id: "ai-z", role: "ai", content: "", tool_calls: [{ id: "call-z", name: "read_file", args: {} }] }]);
  const windowedAfter = [...sequenceRows(long)];
  const kept = windowedBefore.filter(row => windowedAfter.includes(row)).length;
  assert.ok(kept >= windowedBefore.length - 4, "窗口滑动只应移除边界上的少数行，其余行保持身份");
});

test("同一序列内出现重复身份键时退化为保守替换且不丢行", () => {
  const harness = newHarness();
  const flow = buildEventOnlyFlow(6);
  renderFresh(harness, flow);
  const before = sequenceRows(harness);
  const rowCount = before.length;

  harness.vm.runInContext(`
    (() => {
      const sequence = document.querySelector("#conversation").querySelector(".conversation-event-sequence");
      const clone = sequence.firstElementChild.cloneNode(true);
      sequence.append(clone);
    })()
  `, harness.context);
  assert.equal(sequenceRows(harness).length, rowCount + 1, "基线：容器内存在两行同键");

  applySnapshot(harness, flow);
  const after = sequenceRows(harness);
  assert.equal(after.length, rowCount, "重复键时必须退化为按目标状态重建，且不丢行");
  const reference = newHarness();
  renderFresh(reference, flow);
  assert.deepStrictEqual(
    [...after.map(row => row.dataset.eventKey)],
    [...sequenceRows(reference).map(row => row.dataset.eventKey)],
    "退化路径的最终行序列必须与一次性写入一致",
  );
});

test("展开态与按需详情体在无关更新中保持，在自身内容变化后重新挂载", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(1);
  renderFresh(harness, flow.slice(0, 3));
  const expanded = harness.vm.runInContext(`(() => {
    const row = document.querySelector("#conversation").querySelector(".conversation-event.is-tool");
    row.open = true;
    row.dispatchEvent({ type: "toggle" });
    return { filled: row.querySelector(".conversation-event-detail").innerHTML.length };
  })()`, harness.context);
  assert.ok(expanded.filled > 0, "展开后详情体应被按需填充");

  applySnapshot(harness, flow.slice(0, 4));
  const kept = nodeState(harness, ".conversation-event.is-tool");
  assert.equal(kept.open, true, "无关更新后展开态必须保持");
  assert.ok(kept.detailLength > 0, "无关更新后详情体必须仍然有内容");

  const changed = flow.slice(0, 4).map(message => (
    message.id === "tool-0" ? { ...message, content: "结果 0（第二版，内容更长）" } : message
  ));
  applySnapshot(harness, changed);
  const remounted = nodeState(harness, ".conversation-event.is-tool");
  assert.equal(remounted.open, true, "自身内容变化后展开态必须保持");
  assert.ok(remounted.detailLength > 0, "自身内容变化后详情体必须按新内容重新挂载");
});

test("对账的计划阶段不改变活动容器", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(2);
  renderFresh(harness, flow.slice(0, 3));
  harness.context.__messages = flow.slice(0, 5);
  const observed = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const before = container.outerHTML;
    const original = conversationReconciler.diffUnits;
    let atDecision = null;
    conversationReconciler.diffUnits = (prev, next) => {
      if (atDecision === null) atDecision = container.outerHTML;
      return original(prev, next);
    };
    try { replaceConversation(__task, __messages); }
    finally { conversationReconciler.diffUnits = original; }
    return { sameAtDecision: atDecision === before, changedAfterApply: container.outerHTML !== before };
  })()`, harness.context);
  assert.equal(observed.sameAtDecision, true, "决策前活动容器必须与写入前逐字节相同");
  assert.equal(observed.changedAfterApply, true, "应用阶段应真正写入变化");
});

test("非序列单元内容变化后不留旧节点", () => {
  const harness = newHarness();
  const first = [
    { id: "h-1", role: "human", content: "开始" },
    { id: "a-1", role: "ai", content: "第一版正文" },
  ];
  renderFresh(harness, first);
  const base = snapshot(harness);

  const second = [
    { id: "h-1", role: "human", content: "开始" },
    { id: "a-1", role: "ai", content: "第二版正文，更长一些" },
  ];
  applySnapshot(harness, second);
  const after = snapshot(harness);
  assert.equal(after.childCount, base.childCount, "内容变化不得改变容器子节点数（不得留下旧节点）");
  assert.equal(
    harness.vm.runInContext("document.querySelector('#conversation').querySelectorAll('[data-message-key=\"a-1\"]').length", harness.context),
    1,
    "同一身份键的单元在容器中必须唯一",
  );
});

test("无身份键且未声明保留的裸节点在下一次写入中被显式丢弃", () => {
  const harness = newHarness();
  const flow = buildExecutionFlow(1);
  renderFresh(harness, flow.slice(0, 3));
  const injected = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const stray = document.createElement("div");
    stray.className = "unknown-injected";
    container.append(stray);
    globalThis.__stray = stray;
    return { added: container.children.length };
  })()`, harness.context);
  assert.ok(injected.added > 0, "基线：裸节点已进入容器");

  applySnapshot(harness, flow.slice(0, 4));
  const after = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    return {
      stillAttached: [...container.children].includes(__stray),
      matches: container.querySelectorAll(".unknown-injected").length,
    };
  })()`, harness.context);
  assert.equal(after.stillAttached, false, "无身份键且未声明保留的节点必须被显式丢弃，不得滞留");
  assert.equal(after.matches, 0, "被丢弃的节点不得留在容器子树中");
});

test("测试脚手架对会话容器提供真实子节点语义", () => {
  const harness = newHarness();
  const observed = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const first = document.createElement("p");
    const second = document.createElement("p");
    container.append(first);
    container.insertBefore(second, first);
    const ordered = [...container.children].map(node => (node === second ? "second" : "first"));
    const attached = first.parentNode === container && second.parentNode === container;
    second.remove();
    const afterRemove = container.children.length;
    container.replaceChildren();
    return {
      ordered,
      attached,
      afterRemove,
      afterReplaceChildren: container.children.length,
      queryable: typeof container.querySelectorAll === "function" && typeof container.matches === "function",
    };
  })()`, harness.context);
  assert.deepStrictEqual([...observed.ordered], ["second", "first"], "insertBefore 必须真实改变子节点顺序");
  assert.equal(observed.attached, true, "append/insertBefore 必须真实建立父子关系");
  assert.equal(observed.afterRemove, 1, "remove 必须真实摘除子节点");
  assert.equal(observed.afterReplaceChildren, 0, "replaceChildren 必须真实清空子节点");
  assert.equal(observed.queryable, true, "会话容器必须提供选择器查询与匹配语义，不得以空操作代替");
});

test("会话容器的直接写入只发生在声明允许的两类", () => {
  const source = require("node:fs").readFileSync(path.resolve(__dirname, "app.js"), "utf8");
  const writes = [...source.matchAll(/conversation(?:Node\(\))?\??\.(append|prepend|insertBefore)\(([^;]*)/g)];
  assert.ok(writes.length > 0, "基线：app.js 仍会插入声明保留的面板");
  for (const [, method, args] of writes) {
    assert.match(args, /[Pp]anel/, `app.js 的 ${method} 只能插入声明保留的面板，实际参数：${args.trim().slice(0, 60)}`);
  }
  assert.doesNotMatch(source, /work-record message ai streaming/, "流式占位必须由会话写入模块创建，app.js 不得自建占位节点");
});

test("无 id 会话在窗口滑动时保持单元身份", () => {
  const harness = newHarness();
  const long = [];
  for (let index = 0; index < 200; index += 1) {
    long.push({ role: "human", content: `人 ${index}` });
    long.push({ role: "ai", content: `答 ${index}` });
  }
  renderFresh(harness, long);
  harness.context.__long = long;
  const report = harness.vm.runInContext(`(() => {
    const container = document.querySelector("#conversation");
    const byKey = () => new Map([...container.children]
      .filter(node => node.getAttribute("data-message-key") !== null)
      .map(node => [node.getAttribute("data-message-key"), node]));
    const before = byKey();
    replaceConversation(__task, __long.concat([{ role: "ai", content: "新增" }]));
    const after = byKey();
    let kept = 0;
    for (const [key, node] of before) if (after.get(key) === node) kept += 1;
    return { before: before.size, kept };
  })()`, harness.context);
  assert.ok(report.before > 0, "窗口内应挂载消息单元");
  assert.ok(
    report.kept >= report.before - 1,
    `窗口滑动时消息单元身份应保持（期望至少 ${report.before - 1}，实际 ${report.kept}，窗口 ${WINDOW_LIMIT}）`,
  );
});
