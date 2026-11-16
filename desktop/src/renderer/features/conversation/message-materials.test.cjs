"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const materials = require("./message-materials.js");
const { createAppHarness, createNode, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

function fixture() {
  const harness = createAppHarness({ selectors: { "#mainInput": createNode({ value: "draft" }) } });
  const run = code => harness.vm.runInContext(code, harness.context);
  run(readAppSource());
  run(`state.activeTaskId = "current"; state.tasks = [{ task_id: "current", workspace_id: "workspace" }];`);
  return { ...harness, run };
}

test("inherited attachment cards remain readable without offering historical branch actions", () => {
  const { context, run } = fixture();
  context.question = { id: "question", role: "human", content: 'Question\n\n<focus_run_materials>\n' + JSON.stringify([
    { material_id: "ancestor-image", relative_path: "figure.png", material_kind: "image", note: "read diagram" },
  ]) + '\n</focus_run_materials>' };
  const html = run("renderMessage(question, { historyActions: true })");
  assert.match(html, /figure\.png/);
  assert.match(html, /read diagram/);
  assert.match(html, /copy-question/);
  assert.doesNotMatch(html, /focus_run_materials|edit-history-message|retry-history-message/);
  assert.deepEqual(materials.materialBindings(context.question).map(item => item.ordinal), [0]);
});

test("answer actions retain copying and quoting without regeneration through a derived Context", () => {
  const { run } = fixture();
  const html = run('renderMessage({ id: "answer", role: "ai", content: "Answer" }, { historyActions: true })');
  assert.match(html, /copy-answer/);
  assert.match(html, /quote-history-message/);
  assert.doesNotMatch(html, /retry-history-message|edit-history-message/);
});

test("malformed inherited attachment manifests do not become accepted records", () => {
  assert.deepEqual(materials.materialBindings({ content: "ordinary question" }), []);
  assert.throws(() => materials.materialBindings({ content: "<focus_run_materials>\ninvalid\n</focus_run_materials>" }), /附件记录/);
  assert.throws(() => materials.materialBindings({ content: '<focus_run_materials>\n[{"material_id":"missing-path"}]\n</focus_run_materials>' }), /不完整/);
});
