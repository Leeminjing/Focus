const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const { context } = createAppHarness();
new vm.Script(readAppSource()).runInContext(context);

const order = new vm.Script(`
  (() => {
    setProgress = () => {};
    const tracePanel = { name: "trace", isConnected: false };
    const reviewPanel = { name: "review", isConnected: true };
    const conversation = {
      children: [reviewPanel],
      get lastElementChild() {
        return this.children.at(-1) || null;
      },
      append(node) {
        this.children = this.children.filter(child => child !== node);
        this.children.push(node);
        node.isConnected = true;
      },
      insertBefore(node, before) {
        this.children = this.children.filter(child => child !== node);
        const index = before ? this.children.indexOf(before) : -1;
        if (index < 0) this.children.push(node);
        else this.children.splice(index, 0, node);
        node.isConnected = true;
      },
    };

    state.activeTaskId = "task-a";
    state.commitment.taskId = "task-a";
    state.commitment.tracePanel = tracePanel;
    state.commitment.reviewPanel = reviewPanel;

    restoreCommitmentPanels(conversation);
    return conversation.children.map(node => node.name);
  })()
`).runInContext(context);

assert.deepEqual([...order], ["trace", "review"]);
