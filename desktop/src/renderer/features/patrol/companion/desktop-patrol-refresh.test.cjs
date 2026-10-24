/* 本文件通过生产面板脚本验证隐藏窗口的刷新预算、独立进度更新与失效读取取消。
 * 输入为受控 Live 发布、宿主显隐事件和固定 Revision 回答；输出为 HTTP 调用、DOM 身份及 AbortSignal 断言。
 * 最小 DOM 不模拟 Electron 显隐；宿主事件和初始 ready 状态由宿主测试另行覆盖。
 * 示例：node --test desktop/src/renderer/features/patrol/companion/desktop-patrol-refresh.test.cjs。
 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createDocument, createElement } = require("../../../../../tests/helpers/test-dom.cjs");
const Feedback = require("../workspace/workspace-patrol-feedback.js");
const Inputs = require("../workspace/workspace-patrol-inputs.js");
const Facts = require("../../loop/loop-facts-view.js");
const markdownit = require("../../../vendor/markdown-it.min.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const conversation = text => ({ messages: [{ message: { role: "assistant", content: text } }] });
const projection = (revision = "r1", patch = {}) => ({ loop_id: "loop-a", loop: { state: { status: "running" } },
  contexts: { c: { entity_id: "c", updated_sequence: 1, state: { title: "回答", current_revision_id: revision } } },
  task_progress: { revision: 1, state: { document: { items: [] } } }, interventions: {}, ...patch });

async function panel({ visible = true, workspaceId = "a", choose = async () => null, read = async (_loop, _context, options) => conversation(options.revisionId), history = async () => ({ items: [] }), waitForActivation = true, manualFrames = false } = {}) {
  const body = createElement("body");
  body.innerHTML = fs.readFileSync(path.join(__dirname, "../../../desktop-patrol.html"), "utf8");
  const document = { ...createDocument(), body, querySelector: selector => body.querySelector(selector), hidden: false,
    addEventListener: (...args) => body.addEventListener(...args) };
  body.querySelector("#history-items").ownerDocument = document;
  let state = { projection: null, connection: { status: "live" } }, listener;
  const events = {}, calls = [], states = [], stores = new Map(), copied = [], starts = [], loopReads = [], submissions = [];
  document.fonts = { addEventListener:(name,callback) => { events.fontsLoaded = callback; } };
  const sizing = { width:262, scrollHeight:36, feedbackHeight:0, reads:0, requests:[] }, frames = new Map();
  let frameId = 0;
  const input = body.querySelector("#instruction"), host = body.querySelector("#panel"), properties = new Map();
  Object.defineProperty(input, "style", { value:{height:"36px"} });
  Object.defineProperty(input, "clientWidth", { get:() => sizing.width });
  Object.defineProperty(input, "scrollHeight", { get:() => { sizing.reads++; return sizing.scrollHeight; } });
  Object.defineProperty(host, "style", { value:{ getPropertyValue:name => properties.get(name) || "", setProperty:(name,value) => properties.set(name,value) } });
  body.querySelector(".panel-header").offsetHeight = 35;
  Object.defineProperty(body.querySelector("#compose"), "offsetHeight", { get:() => parseFloat(input.style.height)+33 });
  Object.defineProperty(body.querySelector(".panel-feedback-content"), "offsetHeight", { get:() => sizing.feedbackHeight });
  const store = id => { if (!stores.has(id)) stores.set(id, Inputs.create()); return stores.get(id); };
  const api = { workspacePatrol: async id => { loopReads.push(id); return { loop_id: `loop-${id}` }; }, workspaceInputs: history,
    conversation: (...args) => { calls.push(args); return read(...args); } };
  const bridge = { enabled: true, onVisibility: callback => { events.visibility = callback; },
    onOpen: callback => { events.open = callback; }, onContent() {},
    ready: async () => ({ state: { workspaceId }, panelVisible: visible }),
    setState: async value => { states.push(value); }, resizePanel: async value => { sizing.requests.push(value); } };
  const intake = { store, hasUnfinished: () => false, isSending: () => false, remember() {}, claimUnbound() {},
    flush: async () => {},
    readBinding: () => null, reconcile: async () => {},
    workspace: async id => ({ workspace_id: id, display_name: id }), resourceLimits: async () => ({}),
    choose, bind: async path => ({ workspace_id: path, display_name: path }),
    submit: async id => { submissions.push({ workspaceId: id, content: store(id).get().draft.content }); return { loop_id: `loop-${id}` }; },
    history: (id, page) => store(id).history(page) };
  const window = { markdownit, focusDesktop: { runtime: () => ({ apiBase: "http://fixture", session: "test-session" }), patrol: bridge }, addEventListener:(name,callback) => { events[name] = callback; } };
  const context = vm.createContext({ window, document, markdownit, setTimeout, location: { search: "?surface=panel" }, URLSearchParams,
    AbortController, navigator: { clipboard: { writeText: async text => copied.push(text) } },
    requestAnimationFrame: callback => { if (!manualFrames) return setImmediate(callback); frames.set(++frameId,callback); return frameId; },
    cancelAnimationFrame: id => { if (manualFrames) frames.delete(id); else clearImmediate(id); },
    getComputedStyle: node => node === host ? {paddingTop:"8"} : {paddingTop:"2",paddingBottom:"1",borderTopWidth:"1",borderBottomWidth:"1"},
    screen:{availHeight:1040}, matchMedia:() => ({matches:false}), localStorage: {},
    ResizeObserver: class { constructor(callback) { events.resizeObserved = callback; } observe() {} disconnect() {} },
    FocusLoopApi: { create: () => api }, FocusLoopLiveStore: { create: () => ({ get: () => state, subscribe: callback => { listener = callback; callback(state); } }) },
    FocusLoopLiveConnection: { create: () => ({ start: async id => { starts.push(id); }, stop() {} }) },
    FocusWorkspacePatrolIntake: { create: () => intake }, FocusWorkspacePatrolMaterialPreview: { create: () => ({ render() {}, dispose() {} }) },
    FocusWorkspacePatrolBudget: { markup: () => "", create: () => ({ update() {}, syncDraft() {}, dispose() {} }) },
    FocusAccessMode: { readNewSessionDefault() {} }, FocusWorkspacePatrolFeedback: Feedback, FocusWorkspacePatrolInputs: Inputs, FocusLoopFactsView: Facts,
    FocusLoopLiveSelectors: { selectFacts: value => value.files || [] } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "./desktop-patrol-references.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../files/file-links.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../files/artifact-drawer.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../workspace/patrol-input-references.js"), "utf8"), context);
  const work = { bindings: 0 };
  const InputReferences = context.FocusPatrolInputReferences;
  context.FocusPatrolInputReferences = { ...InputReferences, create(options) {
    const references = InputReferences.create(options);
    return { ...references, bind(model) { work.bindings++; references.bind(model); } };
  } };
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../../shared/markdown/message-reading.js"), "utf8"), context);
  window.FocusMessageReading = context.FocusMessageReading;
  const activated = vm.runInContext(fs.readFileSync(path.join(__dirname, "./desktop-patrol-panel.js"), "utf8"), context);
  if (waitForActivation) await activated;
  else await tick();
  return { calls, states, body, events, api, copied, store, starts, loopReads, activated, context, submissions, work, sizing,
    flushLayout() { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback()); },
    async emit(value, connection = state.connection) { state = { ...state, projection: value, connection }; listener(state); await tick(); },
    async visibility(value) { events.visibility?.(value); await tick(); },
    results: () => body.querySelector("#results") };
}

test("panel batches input measurements, deduplicates layout noise and refits on width changes", async () => {
  const f = await panel({manualFrames:true});
  f.flushLayout();
  assert.equal(f.sizing.requests.length,1);
  const reads = f.sizing.reads;
  for (let i=0;i<100;i++) {
    f.store("a").edit({content:`draft-${i}`});
    f.events.resizeObserved();
  }
  assert.equal(f.sizing.reads,reads,"同一帧的输入不能同步反复读布局");
  f.sizing.scrollHeight = 72;
  f.flushLayout();
  assert.equal(f.sizing.reads,reads+1);
  assert.equal(f.sizing.requests.length,2);
  assert.equal(f.sizing.requests.at(-1).height,161);
  assert.equal(f.sizing.requests.at(-1).animate,undefined);
  for (const height of [1,0,1,0]) {
    f.sizing.feedbackHeight = height; f.events.resizeObserved(); f.flushLayout();
  }
  assert.equal(f.sizing.requests.length,2,"1 DIP 测量波动不能产生 IPC 请求");
  assert.equal(f.sizing.reads,reads+1,"内容和宽度未变时不重测输入框");
  f.sizing.feedbackHeight = 2; f.events.resizeObserved(); f.flushLayout();
  assert.equal(f.sizing.requests.at(-1).height,163,"累计增长不能被容差吞掉");
  f.sizing.feedbackHeight = 0; f.sizing.width = 494; f.sizing.scrollHeight = 36;
  f.events.resize(); f.flushLayout();
  assert.equal(f.sizing.requests.at(-1).height,125,"窗口变宽后应重新测量换行并缩回");
  f.sizing.feedbackHeight = 1000; f.events.resizeObserved(); f.flushLayout();
  assert.equal(f.sizing.requests.at(-1).height,516);
  const count = f.sizing.requests.length;
  f.sizing.feedbackHeight = 2000; f.events.resizeObserved(); f.flushLayout();
  assert.equal(f.sizing.requests.length,count,"超出高度上限的反馈增长不能反复发送尺寸");
  const cachedReads = f.sizing.reads;
  f.context.window.devicePixelRatio = 1.5; f.events.resize(); f.flushLayout();
  assert.equal(f.sizing.reads,cachedReads+1,"切换系统缩放必须使输入测量缓存失效");
  f.events.fontsLoaded(); f.flushLayout();
  assert.equal(f.sizing.reads,cachedReads+2,"字体加载完成后必须重新测量输入换行");
  f.store("a").edit({content:"pending"}); f.events.beforeunload(); f.flushLayout();
  assert.equal(f.sizing.requests.length,count,"销毁页面时必须取消排队的尺寸测量");
});

test("onboarding survives cancelling the folder picker and reaches the first explicit send", async () => {
  let selections = 0;
  const f = await panel({ workspaceId: "", choose: async () => ++selections === 1 ? null : "first-project" });
  await f.events.open({ setupWorkspace: true });
  assert.equal(selections, 1);
  assert.equal(f.body.querySelector("#first-goal").hidden, false);
  assert.match(f.body.querySelector("#first-goal-help").textContent, /模型已验证可用/);
  assert.equal(f.submissions.length, 0);
  f.body.querySelector("#first-goal-folder").dispatchEvent({ type: "click" }); await tick();
  assert.equal(selections, 2);
  assert.equal(f.body.querySelector("#binding-name").textContent, "first-project");
  assert.match(f.body.querySelector("#first-goal-help").textContent, /输入第一个目标/);
  assert.equal(f.submissions.length, 0, "绑定项目不能自动发送");
  const input = f.body.querySelector("#instruction");
  input.value = "整理这个项目的待办事项";
  f.body.querySelector("#compose").dispatchEvent({ type: "submit", preventDefault() {} }); await tick();
  assert.deepEqual(f.submissions, [{ workspaceId: "first-project", content: "整理这个项目的待办事项" }]);
  assert.equal(f.body.querySelector("#first-goal").hidden, true);
});

test("a folder picker failure leaves onboarding with an actionable retry", async () => {
  const f = await panel({ workspaceId: "", choose: async () => { throw new Error("目录选择失败"); } });
  await f.events.open({ setupWorkspace: true });
  assert.equal(f.body.querySelector("#first-goal").hidden, false);
  assert.equal(f.body.querySelector("#first-goal-folder").disabled, false);
  assert.match(f.body.querySelector("#error").textContent, /目录选择失败/);
  assert.equal(f.submissions.length, 0);
});

test("initial history failure leaves panel Live connected and offers a history-only retry", async () => {
  let reads = 0;
  const f = await panel({ history: async () => { if (++reads === 1) throw new Error("HTTP 503"); return { items: [] }; } });
  assert.deepEqual(f.starts, ["loop-a"]);
  assert.equal(f.body.querySelector("#error").textContent, "");
  assert.match(f.body.querySelector("#history-status").textContent, /503/);
  await f.emit(projection());
  assert.equal(f.body.querySelector("#toggle-work").disabled, false);
  assert.match(f.results().textContent, /r1/);
  f.body.querySelector("#retry-history").dispatchEvent({ type: "click" }); await tick();
  assert.equal(reads, 2);
  assert.equal(f.body.querySelector("#history-status").hidden, true);
  assert.deepEqual(f.loopReads, ["a"]);
  assert.deepEqual(f.starts, ["loop-a"]);
});

test("pending panel history allows Live results and controls, then coalesces incoming history updates", async () => {
  let release, reads = 0;
  const f = await panel({ waitForActivation: false, history: () => {
    if (++reads === 1) return new Promise(resolve => { release = resolve; });
    return Promise.resolve({ items: [{ submission_id: "one", content: "latest input" }], next_before: null });
  } });
  assert.deepEqual(f.starts, ["loop-a"]);
  await f.emit(projection());
  await f.emit(projection("r1", { interventions: { updated: {} } }));
  assert.equal(f.body.querySelector("#toggle-work").disabled, false);
  assert.match(f.results().textContent, /r1/);
  assert.equal(reads, 1);
  release({ items: [{ submission_id: "one", content: "old input" }], next_before: null });
  await f.activated; await tick();
  assert.equal(reads, 2);
  assert.match(f.body.querySelector("#history-items").textContent, /latest input/);
});

test("late panel history refresh failures cannot affect another workspace", async () => {
  const f = await panel();
  await f.emit(projection());
  let rejectOld;
  f.api.workspaceInputs = id => id === "a" ? new Promise((_resolve, reject) => { rejectOld = reject; }) : Promise.resolve({ items: [] });
  await f.emit(projection("r1", { interventions: { updated: {} } }));
  await f.events.open({ workspaceId: "b" });
  rejectOld(new Error("old history 503")); await tick();
  assert.equal(f.body.querySelector("#history-status").hidden, true);
  assert.equal(f.body.querySelector("#error").textContent, "");
  assert.deepEqual(f.starts, ["loop-a", "loop-b"]);
});

test("panel controls follow authoritative status and connection, including waiting for new input", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work"), stop = f.body.querySelector("#stop-work");
  assert.equal(toggle.hidden, true);
  assert.equal(stop.disabled, true);
  await f.emit(projection());
  assert.equal(toggle.hidden, false);
  assert.equal(toggle.textContent, "暂停");
  assert.equal(toggle.disabled, false);
  assert.equal(stop.closest(".panel-menu") != null, true);
  await f.emit(projection("r1", { loop: { state: { status: "waiting_user", waiting_reason: "awaiting_input" } } }));
  assert.equal(toggle.disabled, false);
  assert.equal(f.body.querySelector("#activity").hidden, false);
  await f.emit(projection("r1", { loop: { state: { status: "waiting_user", waiting_reason: "approval" } } }));
  assert.equal(toggle.hidden, true);
  assert.equal(stop.disabled, false);
  await f.emit(projection("r1", { loop: { state: { status: "paused" } } }));
  assert.equal(toggle.textContent, "继续");
  assert.equal(toggle.getAttribute("aria-label"), "继续当前工作");
  await f.emit(projection(), { status: "reconnecting" });
  assert.equal(toggle.disabled, true);
  assert.equal(stop.disabled, true);
  for (const status of ["stopped", "completed", "failed"]) {
    await f.emit(projection("r1", { loop: { state: { status } } }), { status: "live" });
    assert.equal(toggle.hidden, true);
    assert.equal(stop.disabled, true);
  }
});

test("pause, resume and secondary stop reuse control API with separate requesting, accepted and applied feedback", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work"), stop = f.body.querySelector("#stop-work");
  const message = f.body.querySelector("#control-feedback"), commands = [];
  let accept;
  f.api.control = (id, command) => { commands.push([id, command]); return new Promise(resolve => { accept = resolve; }); };
  const emitStatus = (status, revision) => f.emit(projection("r1", { loop: { state: { status, revision } } }));
  await emitStatus("running", 1);
  toggle.dispatchEvent({ type: "click" });
  assert.match(message.textContent, /暂停请求中/);
  toggle.dispatchEvent({ type: "click" }); stop.dispatchEvent({ type: "click" });
  assert.equal(commands.length, 1, "在途操作阻止重复点击和冲突命令");
  accept({ revision: 3 }); await tick();
  assert.match(message.textContent, /暂停已受理/);
  assert.equal(toggle.textContent, "暂停", "受理不能乐观切换成继续");
  await emitStatus("paused", 2);
  assert.match(message.textContent, /已受理/, "旧投影不能确认本次操作");
  await emitStatus("paused", 3);
  assert.match(message.textContent, /暂停已生效/);
  assert.equal(toggle.textContent, "继续");
  assert.equal(toggle.disabled, false);
  toggle.dispatchEvent({ type: "click" });
  accept({ revision: 4 }); await tick();
  await emitStatus("running", 4);
  assert.match(message.textContent, /继续已生效/);
  const menu = stop.closest(".panel-menu"); menu.open = true;
  stop.dispatchEvent({ type: "click" });
  assert.equal(menu.open, false);
  accept({ revision: 5 }); await tick();
  await emitStatus("stopped", 5);
  assert.match(message.textContent, /停止已生效/);
  assert.equal(toggle.hidden, true);
  assert.equal(stop.disabled, true);
  assert.deepEqual(commands, [["loop-a", "pause"], ["loop-a", "resume"], ["loop-a", "stop"]]);
});

test("Live can confirm a control before its HTTP response without allowing another request in flight", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work");
  let accept;
  f.api.control = () => new Promise(resolve => { accept = resolve; });
  await f.emit(projection()); toggle.dispatchEvent({ type: "click" });
  await f.emit(projection("r1", { loop: { state: { status: "paused", revision: 2 } } }));
  assert.equal(toggle.disabled, true);
  accept({ revision: 2 }); await tick();
  assert.equal(toggle.disabled, false);
  assert.match(f.body.querySelector("#control-feedback").textContent, /暂停已生效/);
});

test("reconnecting the same loop preserves an in-flight control and its acceptance", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work"), reconnect = f.body.querySelector("#reconnect");
  let accept, calls = 0;
  f.api.control = () => { calls++; return new Promise(resolve => { accept = resolve; }); };
  await f.emit(projection()); toggle.dispatchEvent({ type: "click" });
  reconnect.dispatchEvent({ type: "click" });
  toggle.dispatchEvent({ type: "click" });
  assert.equal(calls, 1);
  accept({ revision: 2 }); await tick();
  reconnect.dispatchEvent({ type: "click" });
  assert.match(f.body.querySelector("#control-feedback").textContent, /已受理/);
  assert.equal(toggle.disabled, true);
  await f.emit(projection("r1", { loop: { state: { status: "paused", revision: 2 } } }));
  assert.match(f.body.querySelector("#control-feedback").textContent, /已生效/);
  assert.equal(toggle.disabled, false);
});

test("failed controls surface errors and restore controls for an explicit retry", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work");
  f.api.control = async () => { throw new Error("控制请求失败"); };
  await f.emit(projection()); toggle.dispatchEvent({ type: "click" }); await tick();
  assert.match(f.body.querySelector("#error").textContent, /控制请求失败/);
  assert.equal(f.body.querySelector("#control-feedback").hidden, true);
  assert.equal(toggle.disabled, false);
  assert.equal(toggle.textContent, "暂停");
  assert.equal(toggle.hasAttribute("aria-busy"), false);
});

test("late control responses and failures cannot affect a different workspace", async () => {
  for (const reject of [false, true]) {
    const f = await panel(), toggle = f.body.querySelector("#toggle-work");
    let finish;
    f.api.control = () => new Promise((resolve, fail) => { finish = () => reject ? fail(new Error("旧工作区失败")) : resolve({ revision: 2 }); });
    await f.emit(projection()); toggle.dispatchEvent({ type: "click" });
    await f.events.open({ workspaceId: "b" });
    assert.equal(toggle.hidden, true, "不得使用上一工作区尚未清除的投影");
    await f.emit(projection("r1", { loop_id: "loop-b" }));
    finish(); await tick();
    assert.equal(f.body.querySelector("#control-feedback").hidden, true);
    assert.equal(f.body.querySelector("#error").textContent, "");
    assert.equal(toggle.disabled, false);
  }
});

test("a newer authoritative state releases an accepted control superseded by other work", async () => {
  const f = await panel(), toggle = f.body.querySelector("#toggle-work");
  f.api.control = async () => ({ revision: 2 });
  await f.emit(projection("r1", { loop: { state: { status: "paused", revision: 1 } } }));
  toggle.dispatchEvent({ type: "click" }); await tick();
  await f.emit(projection("r1", { loop: { state: { status: "waiting_user", waiting_reason: "awaiting_input", revision: 3 } } }));
  assert.equal(f.body.querySelector("#control-feedback").hidden, true);
  assert.equal(toggle.disabled, false);
});

test("typing preserves loaded history DOM until a history change", async () => {
  const f = await panel();
  f.store("a").history({ items: [{ submission_id: "old", content: "existing history", created_at: "2020" }], next_before: null });
  const row = f.body.querySelector("#history-items article");
  assert.ok(row);
  for (let i = 0; i < 100; i++) f.store("a").edit({ content: String(i) });
  assert.equal(f.body.querySelector("#history-items article"), row);
  f.store("a").history({ items: [{ submission_id: "new", content: "new history", created_at: "2021" }], next_before: null });
  assert.notEqual(f.body.querySelector("#history-items article"), row);
});

test("initially hidden panel keeps robot status live without reading answers or mutating results", async () => {
  const f = await panel({ visible: false });
  const results = f.results(), before = [...results.childNodes];
  await f.emit(projection());
  await f.emit(projection("r2", { loop: { state: { status: "completed" } } }));
  assert.equal(f.calls.length, 0, "隐藏面板不能读取回答正文");
  assert.deepEqual(results.childNodes, before);
  assert.equal(f.states.at(-1).state, "completed", "机器人仍需接收轻量状态");
  await f.visibility(true);
  assert.equal(f.calls.length, 1);
  assert.match(results.textContent, /r2/);
});

test("ready answers appear while another context is pending; failed cards retry only their own revision", async () => {
  let release, failed = true;
  const f = await panel({ read: async (_loop, context) => {
    if (context === "slow") return new Promise(resolve => { release = resolve; });
    if (context === "broken" && failed) { failed = false; throw new Error("offline"); }
    return conversation(`${context} 已就绪`);
  } });
  const contexts = Object.fromEntries(["slow", "fast", "broken"].map((id, i) => [id,
    { entity_id: id, updated_sequence: 3-i, state: { title: id, current_revision_id: `r-${id}` } }]));
  await f.emit(projection("r1", { contexts }));
  const answers = f.body.querySelector("#answers");
  assert.match(answers.textContent, /fast 已就绪/, "已就绪回答不能被慢读取阻塞");
  assert.match(answers.textContent, /回答读取失败/);
  const ready = answers.querySelector('[data-answer-context="fast"]');
  const retry = answers.querySelector('[data-answer-context="broken"] button');
  assert.equal(retry.textContent, "重试读取");
  retry.dispatchEvent({ type: "click" }); await tick();
  assert.match(answers.textContent, /broken 已就绪/);
  assert.deepEqual(f.calls.map(call => call[1]), ["slow", "fast", "broken", "broken"]);
  assert.equal(answers.querySelector('[data-answer-context="fast"]'), ready);
  release(conversation("slow 终于返回")); await tick();
  assert.match(answers.textContent, /slow 终于返回/);
  assert.deepEqual(answers.children.map(card => card.dataset.answerContext), ["slow", "fast", "broken"]);
});

test("answers expand as Markdown and copy the full source or an individual code block", async () => {
  const text = '# 可读回答\n\n**结论**\n\n```js\nconst value = 1;\n```\n\n<script>unsafe()</script>';
  const f = await panel({ read: async () => conversation(text) });
  await f.emit(projection());
  const card = f.body.querySelector("#answers article"), reading = card.querySelector(".answer-reading");
  assert.ok(reading, "按需展开阅读区");
  assert.ok(!reading.open);
  reading.open = true; reading.dispatchEvent({ type: "toggle" });
  assert.equal(f.body.querySelector("#panel").dataset.reading, "true");
  assert.equal(reading.querySelector("strong").textContent, "结论");
  assert.equal(reading.querySelector("script"), null);
  const buttons = reading.querySelectorAll("button");
  const fullCopy = buttons.find(button => button.textContent === "复制全文");
  // This minimal DOM retains attribute entities; real Electron checks HTML decoding.
  fullCopy.dataset.copyText = text;
  fullCopy.dispatchEvent({ type: "click", preventDefault() {} }); await tick();
  buttons.find(button => button.textContent === "复制代码").dispatchEvent({ type: "click", preventDefault() {} }); await tick();
  assert.deepEqual(f.copied, [text, 'const value = 1;\n']);
  reading.open = false; reading.dispatchEvent({ type: "toggle" });
  assert.equal(f.body.querySelector("#panel").dataset.reading, "false");
});

test("answer body local links preserve the message's execution source through the shared reader", async () => {
  const mounts = [];
  const f = await panel({ read: async () => ({ messages: [{
    message: { id: "answer", role: "assistant", content: "[报告](reports/report.md)\n\n[中文](<reports/中文 报告.md>)\n\n[编码](reports/%E4%B8%AD%E6%96%87%20%E6%8A%A5%E5%91%8A.md)\n\n[错误](reports/%XY.md)" },
    file_context: { task_id: "c", workspace_id: "a", run_id: "original-run", slot_id: "original-slot" },
  }] }) });
  f.context.FocusFilePreview = { mount: async (_host, options) => mounts.push(JSON.parse(JSON.stringify(options))) };
  await f.emit(projection());
  const reading = f.body.querySelector(".answer-reading"); reading.open = true; reading.dispatchEvent({ type: "toggle" });
  const body = reading.querySelector(".answer-markdown"), links = body.querySelectorAll("a");
  for (const [index, expected] of [[0, "reports/report.md"], [1, "reports/中文 报告.md"], [2, "reports/中文 报告.md"]]) {
    let prevented = false;
    reading.querySelector(".answer-content").dispatchEvent({ type: "click", target: links[index], preventDefault() { prevented = true; }, stopPropagation() {} });
    await tick();
    assert.equal(prevented, true, "相对链接必须在原生导航前拦截");
    assert.equal(mounts.at(-1)?.target.task_id, "c");
    assert.equal(mounts.at(-1)?.target.link.path, expected);
    assert.equal(mounts.at(-1)?.target.link.run_id, "original-run");
    assert.equal(mounts.at(-1)?.target.link.slot_id, "original-slot");
    assert.match(f.body.querySelector("#version-body").textContent, /当前磁盘文件/);
  }
  const count = mounts.length;
  links[3].setAttribute("href", "reports/%XY.md");
  reading.querySelector(".answer-content").dispatchEvent({ type: "click", target: links[3], preventDefault() {}, stopPropagation() {} }); await tick();
  assert.equal(mounts.length, count);
  assert.match(f.body.querySelector("#error").textContent, /文件链接无效/);
});

test("answer links explain missing or conflicting sources without navigating or reading another workspace", async () => {
  for (const file_context of [undefined, { task_id: "c", workspace_id: "foreign", run_id: "r" },
    { error: "回答缺少可验证的文件执行来源；请从文件 / 产物打开" }]) {
    const f = await panel({ read: async () => ({ messages: [{ message: { role: "ai", content: "[报告](report.md)" }, file_context }] }) });
    f.context.FocusFilePreview = { mount: async () => assert.fail("来源不明不得尝试读取文件") };
    await f.emit(projection());
    const reading = f.body.querySelector(".answer-reading"); reading.open = true; reading.dispatchEvent({ type: "toggle" });
    const body = reading.querySelector(".answer-markdown"); let prevented = false;
    reading.querySelector(".answer-content").dispatchEvent({ type: "click", target: body.querySelector("a"), preventDefault() { prevented = true; }, stopPropagation() {} }); await tick();
    assert.equal(prevented, true);
    assert.match(f.body.querySelector("#error").textContent, /文件.*来源/);
    assert.equal(f.body.querySelector("#version-detail").hidden, true);
  }
});

test("historical answer links retain the trusted version and require an explicit switch to disk", async () => {
  const requests = [], mounts = [];
  const ref = { workspace_id: "a", run_id: "old-run", slot_id: "old-tree", fact_id: "old-fact", revision: 2, result_key: "old-result", sha256: "old-hash" };
  const f = await panel({ read: async () => ({ messages: [{ message: { role: "assistant", content: "[报告](output/report.md)" }, file_context: { task_id: "c", ...ref } }] }) });
  f.context.fetch = async (url, options) => {
    requests.push({ url, ...options, reference: JSON.parse(options.body) });
    return new Response(JSON.stringify({ detail: "此历史版本未保存对应内容" }), { status: 409 });
  };
  f.context.FocusFilePreview = { mount: async (_host, options) => mounts.push(JSON.parse(JSON.stringify(options))) };
  await f.emit(projection());
  const reading = f.body.querySelector(".answer-reading");
  reading.open = true; reading.dispatchEvent({ type: "toggle" });
  reading.querySelector("a").dispatchEvent({ type: "click", preventDefault() {}, stopPropagation() {} }); await tick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "http://fixture/desktop/api/tasks/c/file-links/content");
  assert.equal(requests[0].headers["X-Focus-Session"], "test-session");
  assert.deepEqual(requests[0].reference, { ...ref, path: "output/report.md", view: "history" });
  assert.equal(mounts.length, 0, "历史读取失败不能改读当前文件");
  assert.match(f.body.querySelector("#version-body").textContent, /此历史版本未保存对应内容/);
  f.body.querySelector("[data-file-link-current]").dispatchEvent({ type: "click" }); await tick();
  assert.deepEqual(mounts[0].target, { task_id: "c", link: {
    workspace_id: "a", run_id: "old-run", slot_id: "old-tree", path: "output/report.md", view: "current",
  } });
});

test("artifact names open fixed version details and can be referenced without leaving the panel", async () => {
  const file = { path: "reports/result.md", result_key: "result-v1", sha256: "hash-v1", slot_id: "slot-a" };
  const f = await panel();
  const details = [];
  f.api.factDetail = async (...args) => { details.push(args); return { file: { ...file, after: { available: true, text: "产物内容" } } }; };
  await f.emit(projection("r1", { files: [{ fact_id: "artifact-a", revision: 2, kind: "artifact", file }] }));
  const artifacts = f.body.querySelector("#artifacts");
  assert.match(artifacts.textContent, /result.md/);
  artifacts.querySelectorAll("button").find(button => button.textContent === "引用").dispatchEvent({ type: "click" }); await tick();
  assert.equal(f.store("a").get().draft.file_refs[0].result_key, "result-v1");
  assert.equal(f.store("a").get().draft.file_refs[0].revision, 2);
  artifacts.querySelectorAll("button").find(button => button.textContent === "打开").dispatchEvent({ type: "click" }); await tick();
  assert.equal(details[0][0], "loop-a");
  assert.equal(details[0][1], "artifact-a");
  assert.equal(details[0][3], 2);
  assert.equal(f.body.querySelector("#version-detail").hidden, false);
});

test("other contexts and title changes preserve an expanded answer and its Markdown DOM", async () => {
  const f = await panel();
  const p = projection();
  await f.emit(p);
  const reading = f.body.querySelector(".answer-reading");
  reading.open = true; reading.dispatchEvent({ type:"toggle" });
  const markdown = reading.querySelector(".answer-markdown");
  p.contexts.c.state.title = "更新后的标题";
  p.contexts.other = { entity_id:"other", updated_sequence:2, state:{ title:"另一项回答", current_revision_id:"other-r1" } };
  await f.emit(p);
  assert.equal(f.body.querySelector('[data-answer-context="c"] h2').textContent, "更新后的标题");
  assert.equal(f.body.querySelector('[data-answer-context="c"] .answer-reading'), reading);
  assert.equal(reading.open, true);
  assert.equal(reading.querySelector(".answer-markdown"), markdown);
  assert.equal(f.calls.length, 2, "新增 Context 只读取新卡片");
  await f.visibility(false); await f.visibility(true);
  assert.equal(f.body.querySelector('[data-answer-context="c"] .answer-reading'), reading);
  assert.equal(reading.open, true);
  assert.equal(f.calls.length, 2);
  await f.emit(projection("r2"));
  assert.equal(f.body.querySelector("#panel").dataset.reading, "false", "新版本收起旧版本阅读区域");
  assert.doesNotMatch(f.results().textContent, /other-r1/);
});

test("progress and files refresh independently, including while an answer read is pending", async () => {
  let release;
  const f = await panel({ read: () => new Promise(resolve => { release = resolve; }) });
  await f.emit(projection());
  const progress = { revision: 2, state: { document: { items: [{ state: "completed", support: "supported" }] } } };
  await f.emit(projection("r1", { task_progress: progress, files: [{ kind: "file", fact_id: "f", revision: 1 }] }));
  assert.equal(f.calls.length, 1, "进度和文件更新不能重新读取回答");
  assert.match(f.results().textContent, /1 \/ 1/);
  assert.match(f.results().textContent, /1 项产物更新/);
  release(conversation("正文")); await tick();
  const answer = f.results().querySelector("article");
  await f.emit(projection("r1", { task_progress: { ...progress, revision: 3 } }));
  assert.equal(f.calls.length, 1);
  assert.equal(f.results().querySelector("article"), answer, "轻量更新保留回答 DOM");
});

test("hide aborts pending reads; late completion cannot render, report errors or seed the revision cache", async () => {
  const pending = [];
  const f = await panel({ read: () => new Promise(resolve => pending.push(resolve)) });
  await f.emit(projection());
  const signal = f.calls[0][2].signal;
  await f.visibility(false);
  assert.equal(signal.aborted, true);
  pending[0](conversation("过期正文")); await tick();
  assert.doesNotMatch(f.results().textContent, /过期正文/);
  await f.emit(projection("r2"));
  assert.equal(f.calls.length, 1);
  await f.visibility(true);
  assert.equal(f.calls.length, 2);
  pending[1](conversation("最新正文")); await tick();
  assert.match(f.results().textContent, /最新正文/);
  assert.equal(f.body.querySelector("#error").textContent, "");
});

test("new answer revisions cancel superseded reads; only changed revisions are fetched", async () => {
  const pending = [];
  const f = await panel({ read: () => new Promise(resolve => pending.push(resolve)) });
  await f.emit(projection());
  await f.emit(projection("r2"));
  assert.equal(f.calls[0][2].signal.aborted, true);
  pending[1](conversation("新回答")); await tick();
  pending[0](conversation("旧回答")); await tick();
  assert.match(f.results().textContent, /新回答/);
  assert.doesNotMatch(f.results().textContent, /旧回答/);
  await f.visibility(false); await f.visibility(true);
  assert.equal(f.calls.length, 2, "已提交 Revision 在再次显示时复用");
});

test("switching workspace while hidden defers result DOM reset and aborts the previous workspace read", async () => {
  const pending = [];
  const f = await panel({ read: () => new Promise(resolve => pending.push(resolve)) });
  await f.emit(projection());
  await f.visibility(false);
  const before = f.results().innerHTML;
  await f.events.open({ workspaceId: "b" });
  assert.equal(f.calls[0][2].signal.aborted, true);
  await f.emit(projection("r1", { loop_id: "loop-b" }));
  pending[0](conversation("A 的迟到回答")); await tick();
  assert.equal(f.calls.length, 1);
  assert.equal(f.results().innerHTML, before);
  await f.visibility(true);
  assert.equal(f.calls[1][0], "loop-b");
  pending[1](conversation("B 的回答")); await tick();
  assert.match(f.results().textContent, /B 的回答/);
  assert.doesNotMatch(f.results().textContent, /A 的迟到回答/);
});

test("six-context refresh fetches only a changed revision and reuses empty answers and current titles", async () => {
  const calls = [];
  const reader = Feedback.createReader({ conversation: async (...args) => { calls.push(args); return args[1] === "c0" ? { messages: [] } : conversation(args[2].revisionId); } });
  const contexts = Object.fromEntries(Array.from({ length: 6 }, (_, i) => [`c${i}`, { entity_id: `c${i}`, updated_sequence: i, state: { title: `标题${i}`, current_revision_id: "r1" } }]));
  const p = projection("r1", { contexts });
  assert.equal((await reader.results(p)).length, 5);
  contexts.c1 = { ...contexts.c1, state: { title: "新标题", current_revision_id: "r2" } };
  const cards = await reader.results(p);
  assert.equal(calls.length, 7);
  assert.equal(cards.find(item => item.context_id === "c1").title, "新标题");
  assert.equal(calls.filter(item => item[1] === "c0").length, 1, "没有 assistant 正文的不可变版本也应缓存");
  contexts.c1 = { ...contexts.c1, state: { title: "再改标题", current_revision_id: "r2" } };
  assert.equal((await reader.results(p)).find(item => item.context_id === "c1").title, "再改标题");
  assert.equal(calls.length, 7);
  assert.ok(calls.every(item => item[2].limit === 30));
});

test("revision cache isolates loops, retries failed reads and bounds retained versions", async () => {
  let count = 0, failure = true;
  const reader = Feedback.createReader({ conversation: async () => {
    count++;
    if (failure) { failure = false; throw new Error("offline"); }
    return conversation("正文");
  } });
  assert.match((await reader.results(projection()))[0].error, /读取失败/);
  await reader.results(projection()); await reader.results(projection());
  assert.equal(count, 2);
  await reader.results(projection("r1", { loop_id: "loop-b" }));
  assert.equal(count, 3);
  for (let i = 2; i <= 49; i++) await reader.results(projection(`r${i}`));
  const before = count;
  await reader.results(projection());
  assert.equal(count, before + 1, "缓存有界，旧版本可重新读取");
});

test("late cancelled reads cannot poison the cache or surface non-Abort errors", async () => {
  let release, count = 0;
  const reader = Feedback.createReader({ conversation: async () => {
    if (++count === 1) return new Promise(resolve => { release = resolve; });
    return conversation("重新读取");
  } });
  const controller = new AbortController();
  const first = reader.results(projection(), controller.signal);
  controller.abort(); release(conversation("取消后迟到"));
  await assert.rejects(first, { name: "AbortError" });
  assert.equal((await reader.results(projection()))[0].text, "重新读取");
  assert.equal(count, 2);
  const f = await panel({ read: async (_loop, _context, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("late transport failure")));
  }) });
  await f.emit(projection()); await f.visibility(false);
  assert.equal(f.body.querySelector("#error").textContent, "");
});

for (const count of [20, 200, 1000]) {
  test(`panel typing with ${count} inputs skips history requests, construction and reference binding`, async () => {
    let requests = 0;
    const rows = Array.from({ length: count }, (_, i) => ({ submission_id: `s-${i}`, intent_id: `i-${i}`, content: `saved ${i}`, processing_state: "addressed" }));
    const f = await panel({ history: async () => { requests++; return { items: rows, next_before: null }; } });
    const items = f.body.querySelector("#history-items");
    let builds = 0;
    const replace = items.replaceChildren.bind(items);
    items.replaceChildren = (...args) => { builds++; replace(...args); };
    const baseline = f.work.bindings, reads = requests;
    const content = f.body.querySelector("#instruction");
    for (let i = 1; i <= 10; i++) {
      content.value = "typing".repeat(i);
      content.dispatchEvent({ type: "input", target: content });
    }
    await tick();
    assert.equal(requests - reads, 0);
    assert.equal(builds, 0);
    assert.equal(f.work.bindings - baseline, 0);
  });
}

test("panel coalesces affected old IDs during a refresh and retries the same targeted failures", async () => {
  const requests = [];
  const rows = Array.from({ length: 1000 }, (_, i) => ({ submission_id: `s-${i}`, intent_id: `i-${i}`, content: `saved ${i}`, processing_state: "addressed" }));
  let release, fail = true;
  const f = await panel({ history: async (_workspace, _before, options) => {
    requests.push(options?.intentIds || "latest");
    if (requests.length === 2) return new Promise(resolve => { release = resolve; });
    if (options && fail) { fail = false; throw new Error("targeted read failed"); }
    return { items: options ? rows.filter(row => options.intentIds.includes(row.intent_id)) : requests.length === 1 ? rows : [rows[999]], next_before: null };
  } });
  await f.emit(projection());
  await f.emit(projection("r1", { loop: { state: { status: "paused" } }, runs: { unrelated: { state: { status: "success" } } } }));
  assert.deepEqual(requests, ["latest"]);
  await f.emit(projection("r1", { interventions: { "i-999": { revision: 2 } } }));
  await f.emit(projection("r1", { interventions: { "i-999": { revision: 2 }, "i-1": { revision: 2 } } }));
  await f.emit(projection("r1", { interventions: { "i-999": { revision: 2 }, "i-1": { revision: 2 }, "i-2": { revision: 2 } } }));
  assert.deepEqual(requests, ["latest", "latest"]);
  release({ items: [rows[999]], next_before: null }); await tick();
  assert.deepEqual(requests, ["latest", "latest", "latest", ["i-1", "i-2"]]);
  assert.match(f.body.querySelector("#history-status").textContent, /targeted read failed/);
  f.body.querySelector("#retry-history").dispatchEvent({ type: "click" }); await tick();
  assert.deepEqual(requests.slice(-2), ["latest", ["i-1", "i-2"]]);
  assert.equal(f.body.querySelector("#history-status").hidden, true);
  assert.equal(f.store("a").get().rows.length, 1000);
});
