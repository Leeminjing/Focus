"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const controls = require("./session-controls.js");
const { createAppHarness, createNode, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");
const equipment = { default_model_name: "a", models: [
  { name: "a", display_name: "Model A", context_window: 10000, reasoning_efforts: ["low", "high"] },
  { name: "b", display_name: "Model B", context_window: 1000, reasoning_efforts: [] },
] };
const escape = text => String(text).replaceAll("<", "&lt;").replaceAll('"', "&quot;");

test("default and per-session selection resolve independently and unsupported effort is omitted", () => {
  assert.deepEqual(controls.request(equipment), { model_name: "a" });
  assert.deepEqual(controls.request(equipment, { session_model_name: "a", session_reasoning_effort: "high" }),
    { model_name: "a", reasoning_effort: "high" });
  assert.deepEqual(controls.request(equipment, { session_model_name: "b", session_reasoning_effort: "high" }), { model_name: "b" });
  assert.deepEqual(controls.request(equipment, { session_model_name: "deleted" }), { model_name: "deleted" });
  assert.equal(equipment.default_model_name, "a");
});

test("context uses snapshot estimate and selected window, never cumulative run input", () => {
  const detail = { context_usage: { estimated_tokens: 950 }, ui_state: { session_model_name: "b" } };
  const html = controls.render(equipment, detail, { prompt_input_tokens: 80000 }, escape);
  assert.match(html, /约 95% 已用/);
  assert.match(html, /is-warning/);
  assert.doesNotMatch(html, /id="sessionReasoning"/);
  assert.match(html, /80,000/);
  assert.match(controls.render(equipment, { ui_state: { session_model_name: "deleted" } }, null, escape), /已不可用/);
  assert.match(controls.render(equipment, {}, null, escape), /待加载/);
});

test("usage distinguishes unknown values, measured zero, active and settled rounds", () => {
  const html = controls.usage(equipment, { model_name: "a", status: "success", model_call_count: 2,
    prompt_input_tokens: 12000, prompt_output_tokens: 0, prompt_cache_hit_tokens: 9000 }, escape);
  for (const text of ["Model A", "12,000", "9,000", "最近一轮", "<dd>0</dd>"]) assert.ok(html.includes(text));
  assert.match(controls.usage(equipment, { status: "running" }, escape), /尚未完整/);
  assert.match(controls.usage(equipment, null, escape), /尚未运行/);
});

test("real sending freezes model and effort across a lost receipt and later session edits", async () => {
  const input = createNode({ value: "hello" });
  const h = createAppHarness({ fetch: true, selectors: { "#mainInput": input } });
  const run = source => vm.runInContext(source, h.context);
  run(readAppSource());
  h.context.catalog = equipment;
  run(`state.view = 'focus'; state.activeTaskId = 'task'; state.equipment = catalog;
    state.tasks = [{task_id:'task', workspace_id:'ws'}];
    state.details.set('task', {messages:[], ui_state:{session_model_name:'a',session_reasoning_effort:'high'}});
    state.materials.set('task', []);`);
  const bodies = [];
  h.context.fetch = async (url, options) => {
    if (url.endsWith('/main/runs')) { bodies.push(options.body); throw new TypeError('lost receipt'); }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await run('sendMain()');
  run("state.details.get('task').ui_state.session_model_name = 'b'");
  await run('sendMain()');
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0], bodies[1]);
  assert.equal(JSON.parse(bodies[0]).model_name, 'a');
  assert.equal(JSON.parse(bodies[0]).reasoning_effort, 'high');
});
