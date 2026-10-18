"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const reference = require("./conversation-reference.js");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");
const quote = (text = "Redis 🔒") => ({ task_id: "A", message_id: "answer", text, start: 4, end: 4 + text.length, offset_basis: "rendered_text_utf16" });

test("reference drafts survive reload, isolate tasks and keep removal tombstones", () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  const drafts = reference.createDrafts(storage);
  drafts.set("A", quote());
  assert.equal(drafts.get("B"), null);
  assert.deepEqual(reference.createDrafts(storage).get("A"), quote());
  drafts.consume("A", quote("other"));
  assert.deepEqual(drafts.get("A"), quote());
  drafts.consume("A", quote());
  assert.equal(reference.createDrafts(storage).get("A", quote()), null);
});

test("cards escape selected HTML and protocol stripping requires metadata", () => {
  const html = reference.render(quote('<img src=x onerror="alert(1)">'));
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(reference.render(quote(), { editable: false }), /reference-remove/);
  const block = '<focus_message_reference>\n{}\n</focus_message_reference>';
  assert.equal(reference.stripProtocol(`question\n\n${block}`, quote()), "question");
  assert.equal(reference.stripProtocol(block, quote()), "");
  assert.equal(reference.stripProtocol(block), block);
  assert.equal(reference.stripProtocol(`explain\n\n${block}\n\n${block}`, quote()), `explain\n\n${block}`);
});

test("a historical reference change invalidates the conversation row cache", () => {
  const h = setup();
  const before = h.evaluate(`globalThis.detail = {messages:[{role:"human",id:"q",content:"why?",additional_kwargs:{message_reference:quote}}]};
    renderConversation(detail, state.tasks[0])`);
  assert.match(before, /Redis/);
  const after = h.evaluate('detail.messages[0].additional_kwargs.message_reference = {...quote, text:"other 🔒"}; renderConversation(detail, state.tasks[0])');
  assert.match(after, /other/);
  assert.doesNotMatch(after, /Redis/);
});

function setup() {
  const input = { value: "为什么单机也需要？", focus() {} };
  const card = { innerHTML: "" };
  const h = createAppHarness({ selectors: { "#mainInput": input, "#composerReference": card } });
  const evaluate = code => h.vm.runInContext(code, h.context);
  evaluate(readAppSource());
  h.context.quote = quote();
  evaluate(`state.activeTaskId = "A"; state.view = "focus";
    state.tasks = [{task_id:"A",workspace_id:"workspace"}];
    state.details.set("A", {messages:[],ui_state:{}});
    renderFocus = () => {}; persistFocusState = () => {}; listenToRun = () => {};
    globalThis.requests = []; globalThis.fail = false; globalThis.hold = false;
    api = async (path, options) => {
      requests.push({body:options.body, key:options.headers["Idempotency-Key"]});
      if (hold) await new Promise(resolve => globalThis.releaseRequest = resolve);
      if (fail) throw new Error("offline");
      return {run_id:"run",message_id:"question",status:"pending",kind:"main"};
    };
    setMainReference(quote);`);
  return { ...h, input, card, evaluate };
}

test("a branch reference navigates to the origin, reveals its message and restores the selection", async () => {
  const h = setup();
  const calls = [];
  const content = {};
  const article = { dataset: { messageId: "answer" },
    scrollIntoView: () => calls.push("scroll"),
    classList: { add: () => calls.push("highlight"), remove() {} },
    querySelector: () => content };
  h.context.document.querySelectorAll = () => [article];
  h.context.content = content;
  h.context.calls = calls;
  h.evaluate(`state.activeTaskId = 'branch';
    switchTask = async taskId => { calls.push(taskId); state.activeTaskId = taskId; };
    conversationReference.selectRange = (node, selected) => {
      if (node === content && selected.task_id === 'A' && selected.message_id === 'answer') calls.push('selection');
    };`);
  await h.evaluate("jumpToReference(quote)");
  assert.deepEqual(calls, ["A", "scroll", "highlight", "selection"]);
});

test("source navigation does not select a message after a newer navigation takes over", async () => {
  const h = setup();
  h.evaluate(`state.activeTaskId = 'branch';
    switchTask = async () => { state.activeTaskId = 'another'; };
    conversationReference.selectRange = () => { throw new Error('stale selection'); };`);
  await h.evaluate("jumpToReference(quote)");
  assert.equal(h.evaluate("state.activeTaskId"), "another");
});

test("sending includes provenance, displays a historical card and consumes only the accepted quote", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  assert.deepEqual(JSON.parse(h.context.requests[0].body).message_reference, quote());
  assert.equal(h.evaluate('referenceDrafts.get("A")'), null);
  assert.equal(h.evaluate('state.details.get("A").messages[0].additional_kwargs.message_reference.text'), quote().text);
  const markup = h.evaluate('renderMessage(state.details.get("A").messages[0])');
  assert.match(markup, /reference-source/);
  assert.doesNotMatch(markup, /reference-remove/);
});

test("a failed delivery retains its exact quote and idempotency key on retry", async () => {
  const h = setup();
  h.evaluate("fail = true");
  await h.evaluate("sendMain()");
  assert.deepEqual(JSON.parse(JSON.stringify(h.evaluate('mainReference("A")'))), quote());
  // A later draft must not modify the already frozen request.
  h.evaluate('referenceDrafts.set("A", {...quote, text:"next", end:8}); fail = false;');
  await h.evaluate("sendMain(true)");
  assert.equal(h.context.requests[0].body, h.context.requests[1].body);
  assert.equal(h.context.requests[0].key, h.context.requests[1].key);
  assert.equal(h.evaluate('referenceDrafts.get("A").text'), "next");
});

for (const mode of ["steer", "queue"]) test(`${mode} delivery preserves its nested reference on retry and queue editing`, async () => {
  const h = setup();
  h.evaluate(`state.details.get("A").active_run = {run_id:"active",status:"running"};
    const base = api;
    api = async (path, options) => {
      const accepted = await base(path, options);
      const body = JSON.parse(options.body);
      return {input_id:"queued",mode:body.mode,status:body.mode === "queue" ? "queued" : "accepted",run:body.run};
    }; fail = true;`);
  await h.evaluate(`sendMain(false, "${mode}")`);
  assert.deepEqual(JSON.parse(JSON.stringify(h.evaluate('mainReference("A")'))), quote());
  assert.deepEqual(JSON.parse(h.context.requests[0].body).run.message_reference, quote());
  h.evaluate("fail = false");
  await h.evaluate("sendMain(true)");
  assert.deepEqual(h.context.requests[0], h.context.requests[1]);
  assert.equal(h.evaluate('referenceDrafts.get("A")'), null);
  if (mode === "queue") {
    h.context.savedRun = JSON.parse(h.context.requests[0].body).run;
    h.evaluate('api = async () => ({input_id:"queued",run:savedRun}); persistUiState = async () => {};');
    await h.evaluate('withdrawQueuedMain("queued", true)');
    assert.deepEqual(JSON.parse(JSON.stringify(h.evaluate('mainReference("A")'))), quote());
    h.evaluate('setMainReference(null); api = async () => { throw new Error("offline"); };');
    await h.evaluate('sendMain(false, "queue")');
    const frozen = h.evaluate('taskRunOperations.recovery("A")');
    assert.equal(JSON.parse(frozen.body).run.message_reference, undefined);
  }
});

test("switching task during delivery never clears another task's reference", async () => {
  const h = setup();
  h.evaluate("hold = true");
  const sending = h.evaluate("sendMain()");
  h.evaluate('state.activeTaskId = "B"; referenceDrafts.set("B", {...quote, task_id:"B"}); releaseRequest();');
  await sending;
  assert.equal(h.evaluate('referenceDrafts.get("B").task_id'), "B");
  assert.equal(h.input.value, "为什么单机也需要？");
});

test("reference survives attachment preparation and a later reference stays for the next turn", async () => {
  const h = setup();
  h.evaluate(`state.composerAttachments.set("A", new Map([["image", {status:"uploading"}]]));
    prepareMainAttachments = async (_task, selection) => { await new Promise(resolve => globalThis.finishUpload = resolve); return selection; };`);
  const sending = h.evaluate("sendMain()");
  h.context.nextQuote = quote("next");
  h.evaluate('setMainReference(nextQuote); finishUpload();');
  await sending;
  assert.deepEqual(JSON.parse(h.context.requests[0].body).message_reference, quote());
  assert.equal(h.evaluate('referenceDrafts.get("A").text'), "next");
});
