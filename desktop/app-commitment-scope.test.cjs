const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const statusNode = {
  textContent: "",
  classList: { toggle() {} },
};
const input = { value: "你好" };
const inert = {
  addEventListener() {},
  classList: { toggle() {} },
};
const document = {
  body: { dataset: {} },
  addEventListener() {},
  querySelector(selector) {
    if (selector === "#app") return { dataset: {} };
    if (selector === "#globalStatus") return statusNode;
    if (selector === "#mainInput") return input;
    return inert;
  },
};

const context = vm.createContext({
  Headers,
  clearInterval() {},
  clearTimeout() {},
  console,
  document,
  location: { origin: "http://localhost", protocol: "http:" },
  setTimeout() {},
  window: {},
});
const source = fs
  .readFileSync(require.resolve("./app.js"), "utf8")
  .replace(/bootstrap\(\);\s*$/, "");
new vm.Script(source).runInContext(context);

const result = new vm.Script(`
  (async () => {
    const calls = [];
    api = async path => {
      calls.push(path);
      return { run_id: "new-run" };
    };
    renderFocus = () => {};
    persistFocusState = () => {};
    listenToRun = () => {};
    state.tasks = [
      { task_id: "task-a", thread_id: "thread-a" },
      { task_id: "task-b", thread_id: "thread-b" },
    ];
    state.activeTaskId = "task-b";
    state.details.set("task-b", {
      messages: [],
      ui_state: { input: "你好", skills: [] },
    });
    state.commitment.taskId = "task-a";
    state.commitment.review = { stage: 2 };
    state.commitment.recovery = { status: "resumable", stage: 2 };

    setStatus("等待确认");
    reconcileCommitmentRecovery({});
    const statusAfterSwitch = document.querySelector("#globalStatus").textContent;

    await sendMain();
    return {
      calls,
      status: document.querySelector("#globalStatus").textContent,
      statusAfterSwitch,
      activeTaskId: state.activeTaskId,
      commitmentTaskId: state.commitment.taskId,
    };
  })()
`).runInContext(context);

result.then(value => {
  assert.equal(
    JSON.stringify(value.calls),
    JSON.stringify(["/desktop/api/tasks/task-b/main/runs"]),
  );
  assert.notEqual(value.status, "存在尚未处理的承诺流程，请先处理审批面板");
  assert.equal(value.statusAfterSwitch, "");
  assert.equal(value.activeTaskId, "task-b");
}).then(() => new vm.Script(`
  (() => {
    const restored = [];
    showReview = payload => restored.push(payload);
    state.activeTaskId = "task-a";
    state.commitment.taskId = "task-a";
    state.commitment.reviewPanel = {
      isConnected: false,
      dataset: { recoveryStatus: "resumable" },
    };
    const review = {
      type: "commitment_review",
      stage: 2,
      draft: { requirements: ["current"] },
      error: "WorkerOutput EOF",
    };
    mountCommitmentRecovery({
      commitment_recovery: { status: "resumable", stage: 2 },
      pending_commitment_review: review,
    });
    return restored;
  })()
`).runInContext(context)).then(restored => {
  assert.equal(restored.length, 1);
  assert.equal(restored[0].error, "WorkerOutput EOF");
});
