const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

let conversation;
let nextScrollHeight = 1000;

const app = { dataset: {} };
Object.defineProperty(app, "innerHTML", {
  set() {
    let scrollTop = 0;
    conversation = { clientHeight: 100, scrollHeight: nextScrollHeight };
    Object.defineProperty(conversation, "scrollTop", {
      get: () => scrollTop,
      set: value => { scrollTop = Math.min(value, conversation.scrollHeight - conversation.clientHeight); },
    });
  },
});

const { context } = createAppHarness({ selectors: { "#app": app, "#conversation": () => conversation } });
const source = readAppSource();
assert.match(source, /commitment_recovery/);
assert.match(source, /pending_commitment_review/);
assert.match(source, /commitment\/abandon/);
assert.match(source, /放弃旧流程并重开/);
new vm.Script(source).runInContext(context);

new vm.Script(`
  state.tasks = [{ task_id: "task", workspace_path: "C:/workspace", workspace_name: "workspace" }];
  state.activeTaskId = "task";
  state.details.set("task", { messages: [], ui_state: { scrollTop: 0 } });
  renderFocus();
`).runInContext(context);

conversation.scrollTop = 900;
nextScrollHeight = 1300;
new vm.Script(`
  state.details.get("task").messages.push({ role: "human", content: "next run" });
  renderFocus();
`).runInContext(context);

assert.equal(conversation.scrollTop, 1200, "a rerendered conversation that was pinned must stay pinned");
