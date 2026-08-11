const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const statusNode = {
  textContent: "",
  classList: { toggle() {} },
};
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
    return inert;
  },
};
const progressCalls = [];
const traceLabels = [];

const context = vm.createContext({
  Headers,
  clearInterval() {},
  clearTimeout() {},
  console,
  document,
  location: { origin: "http://localhost", protocol: "http:" },
  progressCalls,
  setTimeout() {},
  traceLabels,
  window: {},
});
const source = fs
  .readFileSync(require.resolve("./app.js"), "utf8")
  .replace(/bootstrap\(\);\s*$/, "");
new vm.Script(source).runInContext(context);

const result = new vm.Script(`
  (() => {
    setProgress = (stage, visible) => progressCalls.push({ stage, visible });
    finishTracePanel = label => traceLabels.push(label);
    state.activeTaskId = "task-a";
    state.commitment.taskId = "task-a";
    state.commitment.stage = 7;
    state.commitment.review = null;
    state.commitment.recovery = null;

    advanceCommitmentStage(9);
    settleCommitmentRun("task-a", {
      status: "error",
      error: "read_file target is a directory",
    });

    state.commitment.tracePanel = { isConnected: true };
    restoreCommitmentPanels({ append() {}, insertBefore() {} });

    return {
      stage: state.commitment.stage,
      terminalStatus: state.commitment.terminalStatus,
      terminalError: state.commitment.terminalError,
      status: document.querySelector("#globalStatus").textContent,
    };
  })()
`).runInContext(context);

assert.equal(result.stage, 9);
assert.equal(result.terminalStatus, "error");
assert.equal(result.terminalError, "read_file target is a directory");
assert.equal(result.status, "read_file target is a directory");
assert.deepEqual(traceLabels, ["运行失败"]);
assert.equal(JSON.stringify(progressCalls), JSON.stringify([
  { stage: 9, visible: false },
  { stage: 9, visible: false },
]));
