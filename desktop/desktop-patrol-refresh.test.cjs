/* 本文件通过生产面板脚本验证隐藏窗口的刷新预算、独立进度更新与失效读取取消。
 * 输入为受控 Live 发布、宿主显隐事件和固定 Revision 回答；输出为 HTTP 调用、DOM 身份及 AbortSignal 断言。
 * 最小 DOM 不模拟 Electron 显隐；宿主事件和初始 ready 状态由宿主测试另行覆盖。
 * 示例：node --test desktop/desktop-patrol-refresh.test.cjs。
 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createDocument, createElement } = require("./test-dom.cjs");
const Feedback = require("./workspace-patrol-feedback.js");
const Inputs = require("./workspace-patrol-inputs.js");
const Facts = require("./loop-facts-view.js");
const markdownit = require("./vendor/markdown-it.min.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const conversation = text => ({ messages: [{ message: { role: "assistant", content: text } }] });
const projection = (revision = "r1", patch = {}) => ({ loop_id: "loop-a", loop: { state: { status: "running" } },
  contexts: { c: { entity_id: "c", updated_sequence: 1, state: { title: "回答", current_revision_id: revision } } },
  task_progress: { revision: 1, state: { document: { items: [] } } }, interventions: {}, ...patch });

async function panel({ visible = true, read = async (_loop, _context, options) => conversation(options.revisionId) } = {}) {
  const body = createElement("body");
  body.innerHTML = fs.readFileSync(path.join(__dirname, "desktop-patrol.html"), "utf8");
  const document = { ...createDocument(), body, querySelector: selector => body.querySelector(selector), hidden: false,
    addEventListener: (...args) => body.addEventListener(...args) };
  let state = { projection: null, connection: { status: "live" } }, listener;
  const events = {}, calls = [], states = [], stores = new Map(), copied = [];
  const store = id => { if (!stores.has(id)) stores.set(id, Inputs.create()); return stores.get(id); };
  const api = { workspacePatrol: async id => ({ loop_id: `loop-${id}` }), workspaceInputs: async () => ({ items: [] }),
    conversation: (...args) => { calls.push(args); return read(...args); } };
  const bridge = { enabled: true, onVisibility: callback => { events.visibility = callback; },
    onOpen: callback => { events.open = callback; }, onContent() {},
    ready: async () => ({ state: { workspaceId: "a" }, panelVisible: visible }),
    setState: async value => { states.push(value); }, resizePanel: async () => {} };
  const intake = { store, hasUnfinished: () => false, isSending: () => false, remember() {}, claimUnbound() {},
    readBinding: () => null, reconcile: async () => {},
    workspace: async id => ({ workspace_id: id, display_name: id }), resourceLimits: async () => ({}),
    history: (id, page) => store(id).history(page) };
  const window = { markdownit, focusDesktop: { runtime: () => ({}), patrol: bridge }, addEventListener() {} };
  const context = vm.createContext({ window, document, location: { search: "?surface=panel" }, URLSearchParams,
    AbortController, navigator: { clipboard: { writeText: async text => copied.push(text) } }, requestAnimationFrame: callback => setImmediate(callback), localStorage: {}, ResizeObserver: class { observe() {} disconnect() {} },
    FocusLoopApi: { create: () => api }, FocusLoopLiveStore: { create: () => ({ get: () => state, subscribe: callback => { listener = callback; callback(state); } }) },
    FocusLoopLiveConnection: { create: () => ({ start: async () => {}, stop() {} }) },
    FocusWorkspacePatrolIntake: { create: () => intake }, FocusWorkspacePatrolMaterialPreview: { create: () => ({ render() {}, dispose() {} }) },
    FocusWorkspacePatrolBudget: { markup: () => "", create: () => ({ update() {}, syncDraft() {}, dispose() {} }) },
    FocusAccessMode: { readNewSessionDefault() {} }, FocusWorkspacePatrolFeedback: Feedback, FocusWorkspacePatrolInputs: Inputs, FocusLoopFactsView: Facts,
    FocusLoopLiveSelectors: { selectFacts: value => value.files || [] } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "desktop-patrol-references.js"), "utf8"), context);
  await vm.runInContext(fs.readFileSync(path.join(__dirname, "desktop-patrol-panel.js"), "utf8"), context);
  return { calls, states, body, events, api, copied, store,
    async emit(value) { state = { ...state, projection: value }; listener(state); await tick(); },
    async visibility(value) { events.visibility?.(value); await tick(); },
    results: () => body.querySelector("#results") };
}

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
  buttons.find(button => button.textContent === "复制全文").dispatchEvent({ type: "click" }); await tick();
  buttons.find(button => button.textContent === "复制代码").dispatchEvent({ type: "click" }); await tick();
  assert.deepEqual(f.copied, [text, 'const value = 1;\n']);
  reading.open = false; reading.dispatchEvent({ type: "toggle" });
  assert.equal(f.body.querySelector("#panel").dataset.reading, "false");
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
