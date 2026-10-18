"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const retry = require("./message-retry.js");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");
const messages = [
  { id: "system", role: "system", content: "context" },
  { id: "q1", role: "human", content: "first" },
  { id: "tool-call", role: "ai", content: "", tool_calls: [{ id: "call", name: "read", args: {} }] },
  { id: "tool", role: "tool", tool_call_id: "call", content: "result" },
  { id: "a1", role: "ai", content: "**answer**" },
  { id: "q2", role: "user", content: "second" },
  { id: "a2", role: "assistant", content: "later answer" },
];

test("retrying an answer cuts before its question, preserving earlier complete tool exchanges", () => {
  const original = JSON.stringify(messages);
  for (const id of ["a1", "q1"]) assert.deepEqual(retry.selectTurn(messages, id), { message: messages[1], messages: [messages[0]] });
  assert.deepEqual(retry.selectTurn(messages, "a2").messages, messages.slice(0, 5));
  const branch = retry.selectTurn(messages, "q2");
  branch.messages[0].content = "edited";
  assert.equal(JSON.stringify(messages), original);
  assert.deepEqual(retry.selectTurn([{ id: "first", role: "human", content: "hi" }], "first").messages, []);
});

test("cutting inside nested compressed/deleted history restores only the prefix needed for that question", () => {
  const summary = { id: "summary", role: "ai", content: "keep summary", compression: { source: messages.slice(1, 5) } };
  const nested = { id: "nested", role: "human", content: "deleted", compression: { deleted: true, source: [messages[0], summary, { id: "inner", compression: { source: messages.slice(5) } }] } };
  assert.deepEqual(retry.selectTurn([nested], "a2").messages, [messages[0], summary]);
  assert.deepEqual(retry.selectTurn([nested], "a1").messages, [messages[0]]);
  assert.deepEqual(retry.selectTurn([summary, ...messages.slice(5)], "q2").messages, [summary]);
});

test("missing/ambiguous IDs and assistant messages without a question cannot replay the wrong turn", () => {
  assert.throws(() => retry.selectTurn(messages, "missing"), /唯一定位/);
  assert.throws(() => retry.selectTurn([...messages, messages[1]], "q1"), /唯一定位/);
  assert.throws(() => retry.selectTurn([messages[4]], "a1"), /用户提问/);
  assert.throws(() => retry.selectTurn(messages, "tool"), /不能重新执行/);
  assert.throws(() => retry.selectTurn([{ id: "fake", role: "human", content: "x", curation_synthetic: true }], "fake"), /不能重新执行/);
});

test("editing replaces prose, preserves multimodal blocks and removes the old material protocol", () => {
  const protocol = '\n\n<focus_run_materials>\n[{"material_id":"old"}]\n</focus_run_materials>';
  const image = { type: "image_url", image_url: { url: "data:image/png;base64,bytes" } };
  const question = { content: [{ type: "text", text: "original" }, image, { type: "text", text: protocol }] };
  assert.equal(retry.editableText(question), "original");
  assert.deepEqual(retry.replayContent(question), [{ type: "text", text: "original" }, image]);
  assert.deepEqual(retry.replayContent(question, "changed"), [{ type: "text", text: "changed" }, image]);
  assert.equal(retry.replayContent({ content: "original" + protocol }), "original");
  assert.equal(question.content.length, 3);
});

function setup() {
  const input = createNode({ value: "unsent draft" });
  const editInput = createNode({ value: "changed question" });
  const error = createNode();
  const dialog = createNode({ showModal() {} });
  const harness = createAppHarness({ fetch: true, statusNode: "record", selectors: {
    "#mainInput": input, "#messageRetryInput": editInput, "#messageRetryDialog": dialog, "#messageRetryError": error,
  } });
  const run = code => harness.vm.runInContext(code, harness.context);
  run(readAppSource());
  harness.context.messages = structuredClone(messages);
  run(`state.view = 'focus'; state.activeTaskId = 'source';
    state.tasks = [{ task_id: 'source', title: 'original', workspace_id: 'ws' }];
    state.details.set('source', { messages, ui_state: { input: 'unsent draft', access_mode: 'workspace-write' } });
    composerDraft.claim('source', 'unsent draft');
    renderFocus = () => {}; listenToRun = () => {};
    refreshTasks = async () => { if (!state.tasks.some(task => task.task_id === 'branch')) state.tasks.push({ task_id: 'branch', workspace_id: 'ws' }); };
    hydrateContextTrees = async () => {};
    switchTask = async taskId => { state.activeTaskId = taskId; state.details.set(taskId, { messages: [], ui_state: {} }); };
  `);
  const requests = [];
  harness.context.api = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (url.endsWith("/snapshot")) return { checkpoint_id: "frozen-checkpoint", messages };
    if (url.endsWith("/derive")) return { context_id: "branch", projection_status: "valid" };
    if (url.endsWith("/material-history")) return [];
    if (url.endsWith("/materials")) return [];
    if (url.endsWith("/main/runs")) return { run_id: "new-run", message_id: "new-question", kind: "main", task_id: "branch", status: "pending" };
    return {};
  };
  const button = id => ({ closest: () => ({ dataset: { messageKey: id } }), focus() {} });
  return { ...harness, run, input, editInput, error, requests, button };
}

test("ordinary message controls are role-specific and absent on unrelated/managed surfaces", () => {
  const { run } = setup();
  const user = run("renderMessage(messages[1], { historyActions: true })");
  assert.match(user, /copy-question/); assert.match(user, /edit-history-message/); assert.match(user, /retry-history-message/);
  assert.doesNotMatch(user, /quote-history-message/);
  const answer = run("renderMessage(messages[4], { historyActions: true })");
  assert.match(answer, /quote-history-message/); assert.match(answer, /重新回答/);
  assert.doesNotMatch(answer, /edit-history-message/);
  for (const index of [0, 2, 3]) assert.doesNotMatch(run(`renderMessage(messages[${index}], { historyActions: true })`), /edit-history-message|quote-history-message|retry-history-message/);
  assert.doesNotMatch(run("renderMessage(messages[1])"), /edit-history-message/);
});

test("quote appends a Markdown quote without overwriting the existing composer draft or sending", () => {
  const { context, run, button, input, requests } = setup();
  context.button = button("a1");
  run("quoteHistoryMessage(button)");
  assert.equal(input.value, "unsent draft\n\n> **answer**\n\n");
  assert.equal(run("composerDraft.value('source')"), input.value);
  assert.ok(requests.every(request => !request.url.endsWith("/main/runs")));
});

test("edit derives from the checkpoint prefix, submits only to the branch and preserves source/draft", async () => {
  const { context, run, button, requests } = setup();
  context.button = button("q2");
  await run("openMessageRetry(button, true)");
  assert.equal(requests.length, 1, "editing alone creates no branch");
  await run("submitMessageRetry()");
  const derive = JSON.parse(requests.find(request => request.url.endsWith("/derive")).body);
  assert.deepEqual(derive.sources, [{ context_id: "source", checkpoint_id: "frozen-checkpoint" }]);
  assert.deepEqual(derive.messages, messages.slice(0, 5));
  const main = requests.filter(request => request.url.endsWith("/main/runs"));
  assert.equal(main.length, 1);
  assert.equal(main[0].url, "/desktop/api/tasks/branch/main/runs");
  assert.equal(JSON.parse(main[0].body).message, "changed question");
  assert.equal(JSON.parse(main[0].body).access_mode, "workspace-write");
  assert.equal(run("composerDraft.value('source')"), "unsent draft");
  assert.equal(run("state.details.get('source').messages.length"), messages.length);
});

test("regenerate uses the original question; lost admission keeps the branch request recoverable", async () => {
  const { context, run, button, requests } = setup();
  const originalApi = context.api;
  context.api = async (url, options) => {
    const result = await originalApi(url, options);
    if (url.endsWith("/main/runs")) throw new Error("lost receipt");
    return result;
  };
  context.button = button("a1");
  await run("openMessageRetry(button, false)");
  assert.deepEqual(JSON.parse(requests.find(request => request.url.endsWith("/derive")).body).messages, [messages[0]]);
  const saved = JSON.parse(run("JSON.stringify(taskRunOperations.recovery('branch'))"));
  assert.equal(JSON.parse(saved.body).message, "first");
  assert.equal(saved.failure.uncertain, true);
  assert.equal(run("taskRunOperations.recovery('source')"), null);
});

test("a failed derive leaves edits available, and cancellation creates no branch", async () => {
  const { context, run, button, requests, error, editInput } = setup();
  context.button = button("q2");
  await run("openMessageRetry(button, true)");
  const originalApi = context.api;
  context.api = async (url, options) => { if (url.endsWith("/derive")) throw new Error("offline"); return originalApi(url, options); };
  await run("submitMessageRetry()");
  assert.equal(error.textContent, "offline");
  assert.equal(editInput.value, "changed question");
  assert.equal(run("messageRetryDraft.pending"), false);
  run("closeMessageRetry()");
  assert.equal(run("messageRetryDraft"), null);
  assert.ok(requests.every(request => !request.url.endsWith("/main/runs")));
});

test("replay rebinds attachments to the new task with notes, must-view intent and reading mode", async () => {
  const { context, run, button, requests } = setup();
  const originalApi = context.api;
  context.api = async (url, options) => {
    if (url === '/desktop/api/tasks/source/material-history') return [{ message_id: 'q2', material_id: 'old-image', current_available: true, relative_path: 'image.png', ordinal: 0, note: 'read the labels', must_view_requested: true }];
    if (url === '/desktop/api/tasks/source/materials') return [{ material_id: 'old-image', reading_mode: 'rough', instruction_mode: 'strict' }];
    if (url === '/desktop/api/tasks/branch/materials') { requests.push({ url, ...options }); return { material_id: 'branch-image' }; }
    return originalApi(url, options);
  };
  context.button = button('q2');
  await run('openMessageRetry(button, true)');
  await run('submitMessageRetry()');
  const material = JSON.parse(requests.find(request => request.url.endsWith('/branch/materials')).body);
  assert.deepEqual(material, { path: 'image.png', reading_mode: 'rough', instruction_mode: 'strict' });
  const main = JSON.parse(requests.find(request => request.url.endsWith('/main/runs')).body);
  assert.deepEqual(main.material_inputs, [{ material_id: 'branch-image', note: 'read the labels' }]);
  assert.deepEqual(main.must_view_material_ids, ['branch-image']);
});

test("unavailable original attachments stop replay; retrying preparation reuses the created branch", async () => {
  const { context, run, button, requests, error } = setup();
  const originalApi = context.api;
  let available = false;
  context.api = async (url, options) => {
    if (url === '/desktop/api/tasks/source/material-history') return [{ message_id: 'q2', material_id: 'image', current_available: available, relative_path: 'image.png' }];
    if (url === '/desktop/api/tasks/branch/materials') return { material_id: 'branch-image' };
    return originalApi(url, options);
  };
  context.button = button('q2');
  await run('openMessageRetry(button, true)');
  await run('submitMessageRetry()');
  assert.match(error.textContent, /材料已不可用/);
  assert.equal(requests.filter(request => request.url.endsWith('/main/runs')).length, 0);
  available = true;
  await run('submitMessageRetry()');
  assert.equal(requests.filter(request => request.url.endsWith('/derive')).length, 1);
  assert.equal(requests.filter(request => request.url.endsWith('/main/runs')).length, 1);
});

test("late snapshot responses cannot open an editor after the user navigates away", async () => {
  const { context, run, button } = setup();
  context.button = button('q2');
  let release;
  context.api = () => new Promise(resolve => { release = resolve; });
  const opening = run('openMessageRetry(button, true)');
  run("taskSwitchSequence += 1; state.view = 'map'");
  release({ checkpoint_id: 'cp', messages });
  await opening;
  assert.equal(run('messageRetryDraft'), null);
});

test("inherited question manifests preserve attachments when the current task has no binding rows", async () => {
  const { context, run, button, requests } = setup();
  const inherited = structuredClone(messages);
  inherited[1].content += '\n\n<focus_run_materials>\n' + JSON.stringify([{ material_id: 'ancestor-image', relative_path: 'figure.png', material_kind: 'image', note: 'read diagram', must_view: true }]) + '\n</focus_run_materials>';
  const originalApi = context.api;
  context.api = async (url, options) => {
    if (url.endsWith('/snapshot')) return { checkpoint_id: 'branch-checkpoint', messages: inherited };
    if (url === '/desktop/api/tasks/branch/materials') { requests.push({ url, ...options }); return { material_id: 'new-image' }; }
    return originalApi(url, options);
  };
  context.inherited = inherited;
  const html = run('renderMessage(inherited[1], { historyActions: true })');
  assert.match(html, /figure\.png/); assert.match(html, /read diagram/); assert.match(html, /必须看/);
  assert.doesNotMatch(html, /focus_run_materials|当前内容不可用/);
  context.button = button('a1');
  await run('openMessageRetry(button, false)');
  const main = JSON.parse(requests.find(request => request.url.endsWith('/main/runs')).body);
  assert.equal(main.message, 'first');
  assert.deepEqual(main.material_inputs, [{ material_id: 'new-image', note: 'read diagram' }]);
  assert.deepEqual(main.must_view_material_ids, ['new-image']);
  assert.throws(() => retry.materialBindings({ content: '<focus_run_materials>\ninvalid\n</focus_run_materials>' }), /附件记录/);
});

test("navigation during preparation retains a recoverable branch without hijacking the new view", async () => {
  const { context, run, button, requests } = setup();
  context.button = button('q2');
  await run('openMessageRetry(button, true)');
  run("refreshTasks = async () => { taskSwitchSequence += 1; state.activeTaskId = 'another'; state.view = 'map'; }");
  await run('submitMessageRetry()');
  assert.equal(run('state.activeTaskId'), 'another');
  assert.equal(run('state.view'), 'map');
  assert.equal(run("JSON.parse(taskRunOperations.recovery('branch').body).message"), 'changed question');
  assert.equal(requests.filter(request => request.url.endsWith('/main/runs')).length, 0);
});

test("historical retries retain structured quote provenance while editing only the original prose", async () => {
  const reference = { task_id: 'ancestor', message_id: 'answer', text: 'quoted answer', start_offset: 0, end_offset: 13 };
  const question = { role: 'human', id: 'quoted-q', content: 'follow up\n\n<focus_message_reference>\n' + JSON.stringify(reference) + '\n</focus_message_reference>', additional_kwargs: { message_reference: reference } };
  assert.equal(retry.editableText(question), 'follow up');
  assert.equal(retry.replayContent(question), 'follow up');
  assert.equal(retry.replayContent(question, 'changed'), 'changed');
  for (const edit of [false, true]) {
    const { context, run, button, requests } = setup();
    const originalApi = context.api;
    context.api = async (url, options) => url.endsWith('/snapshot') ? { checkpoint_id: 'quoted-cp', messages: [question] } : originalApi(url, options);
    context.button = button('quoted-q');
    await run(`openMessageRetry(button, ${edit})`);
    if (edit) await run('submitMessageRetry()');
    const body = JSON.parse(requests.find(request => request.url.endsWith('/main/runs')).body);
    assert.equal(body.message, edit ? 'changed question' : 'follow up');
    assert.deepEqual(body.message_reference, reference);
  }
});
