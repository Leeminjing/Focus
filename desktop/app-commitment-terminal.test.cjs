const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const progressCalls = [];
const traceLabels = [];
const { context, document } = createAppHarness({ globals: { progressCalls, traceLabels } });
new vm.Script(readAppSource()).runInContext(context);

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
