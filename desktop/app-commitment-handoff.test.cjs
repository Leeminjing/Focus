const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const { context } = createAppHarness();
new vm.Script(readAppSource()).runInContext(context);

const result = new vm.Script(`
  (() => {
    const progress = [];
    const labels = [];
    setProgress = (stage, visible) => progress.push({ stage, visible });
    finishTracePanel = label => labels.push(label);

    const tracePanel = { name: "trace", isConnected: true, open: true };
    const oldLead = { name: "old-lead", isConnected: true };
    const conversation = {
      children: [oldLead, tracePanel],
      prepend(node) {
        this.children = this.children.filter(child => child !== node);
        this.children.unshift(node);
        node.isConnected = true;
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
      get lastElementChild() { return this.children.at(-1) || null; },
    };
    conversationNode = () => conversation;

    state.activeTaskId = "task-a";
    state.commitment.taskId = "task-a";
    state.commitment.stage = 9;
    state.commitment.tracePanel = tracePanel;
    state.commitment.review = null;
    state.commitment.recovery = null;

    beginLeadExecution("task-a");

    tracePanel.isConnected = false;
    conversation.children = [oldLead];
    restoreCommitmentPanels(conversation);

    return {
      handoffStarted: state.commitment.handoffStarted,
      open: tracePanel.open,
      order: conversation.children.map(node => node.name),
      progress,
      labels,
      toolEvents: conversationEvents.normalize([
        {
          role: "ai",
          content: "",
          tool_calls: [
            { name: "write_file", id: "call-1", args: { path: "src/app.tsx" } },
            { name: "write_file", id: "call-2", args: { path: "src/test.ts" } },
            { name: "list_files", id: "call-3", args: { path: "src" } },
          ],
        },
        { role: "tool", name: "write_file", tool_call_id: "call-1", status: "success", content: "已写入 src/app.tsx" },
      ]).filter(item => item.type === "tool").map(item => conversationEvents.renderEvent(item)),
    };
  })()
`).runInContext(context);

assert.equal(result.handoffStarted, true);
assert.equal(result.open, false);
assert.deepEqual([...result.order], ["trace", "old-lead"]);
assert.deepEqual([...result.labels], ["承诺已完成"]);
assert.equal(result.progress.length, 2);
assert.ok(result.progress.every(call => call.stage === 9 && call.visible === false));
assert.equal(result.toolEvents.length, 3);
assert.equal(result.toolEvents.filter(html => /write_file/.test(html)).length, 2);
assert.equal(result.toolEvents.filter(html => /list_files/.test(html)).length, 1);
assert.ok(result.toolEvents.some(html => /完成/.test(html) && /已写入 src\/app\.tsx/.test(html)));
