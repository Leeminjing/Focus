const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

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

const inert = {
  addEventListener() {},
  classList: { toggle() {} },
  content: { cloneNode() {} },
};
const document = {
  body: { dataset: {} },
  addEventListener() {},
  querySelector(selector) {
    if (selector === "#app") return app;
    if (selector === "#conversation") return conversation;
    return inert;
  },
  querySelectorAll() { return []; },
};

const context = vm.createContext({
  Headers,
  clearTimeout() {},
  console,
  document,
  location: { origin: "http://localhost", protocol: "http:" },
  setTimeout() {},
  window: {},
});
const source = fs.readFileSync(require.resolve("./app.js"), "utf8").replace(/bootstrap\(\);\s*$/, "");
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
