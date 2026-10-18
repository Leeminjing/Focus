/* Drive the production retry/send and source-navigation paths against the test HTTP API. */
"use strict";
const assert = require("node:assert/strict");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");

(async () => {
  const [base, source, action, origin = source] = process.argv.slice(2);
  const harness = createAppHarness({ fetch: true, statusNode: "record", selectors: {
    "#mainInput": createNode(), "#messageRetryInput": createNode({ value: "edited question" }),
    "#messageRetryDialog": createNode({ showModal() {} }), "#messageRetryError": createNode(),
    '[data-action="load-earlier-conversation"]': () => null,
  } });
  const run = code => harness.vm.runInContext(code, harness.context);
  run(readAppSource());
  harness.context.source = source;
  const requests = [];
  harness.context.apiRequests = requests;
  harness.context.api = async (url, options = {}) => {
    if (url.endsWith("/materials") || url.endsWith("/material-history")) return [];
    if (url.endsWith("/access-mode")) return {};
    const response = await fetch(base + url, { ...options, headers: { "Content-Type": "application/json" } });
    const body = await response.json();
    requests.push({ url, status: response.status, body });
    if (!response.ok) throw new Error(JSON.stringify(body));
    return body;
  };
  run(`state.view = 'focus'; state.activeTaskId = source;
    state.tasks = [{ task_id: source, title: 'source', workspace_id: 'ws' }];
    state.details.set(source, { messages: [], ui_state: {} });
    renderFocus = () => {}; listenToRun = () => {};
    refreshTasks = async () => { for (const request of apiRequests) if (request.url.endsWith('/derive') && request.status === 200) state.tasks.push({ task_id: request.body.context_id, workspace_id: 'ws' }); }; hydrateContextTrees = async () => {};
    switchTask = async taskId => { state.activeTaskId = taskId; state.details.set(taskId, { messages: [], ui_state: {} }); };
  `);
  harness.context.button = { closest: () => ({ dataset: { messageKey: action === "regenerate" ? "follow-answer" : "follow-question" } }), focus() {} };
  await run(`openMessageRetry(button, ${action === "edit"})`);
  if (action === "edit") await run("submitMessageRetry()");
  const sent = requests.find(request => request.url.endsWith("/main/runs"));
  assert.ok(sent, JSON.stringify({ requests, status: harness.statusState }));
  assert.equal(sent.status, 200, JSON.stringify(sent.body));
  const reference = sent.body.message_reference;
  assert.equal(reference.task_id, origin);
  assert.equal(reference.message_id, "source-answer");
  assert.equal(reference.text, "Redis 🔒");
  assert.equal(sent.body.message, action === "edit" ? "edited question" : "follow up");
  harness.context.reference = reference;
  const html = run("conversationReference.render(reference, { editable: false })");
  assert.match(html, /Redis 🔒/);
  assert.match(html, /reference-source/);
  await run("jumpToReference(reference)");
  assert.equal(run("state.activeTaskId"), origin, "source link must navigate back to the original conversation");
  process.stdout.write(JSON.stringify(sent.body));
})().catch(error => { console.error(error); process.exitCode = 1; });
