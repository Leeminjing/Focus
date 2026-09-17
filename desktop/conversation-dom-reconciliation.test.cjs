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

function newHarness() {
  const harness = createAppHarness({ fetch: true });
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

test("快照到达后流式占位被回收且不出现重复正文", () => {
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
  assert.equal(
    harness.vm.runInContext("document.querySelector('#conversation').querySelectorAll('[data-stream-run]').length", harness.context),
    1,
    "基线：流式追加应产生一个占位",
  );

  applySnapshot(harness, flow.slice(0, 4), "run-z");
  assert.equal(
    harness.vm.runInContext("document.querySelector('#conversation').querySelectorAll('[data-stream-run]').length", harness.context),
    0,
    "快照已包含该 run 的完成态，流式占位必须被回收",
  );
  assert.equal(harness.vm.runInContext("state.streamBuffers.size", harness.context), 0, "流式缓冲必须被清理");
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
