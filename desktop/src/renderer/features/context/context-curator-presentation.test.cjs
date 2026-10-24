/* 验证 Context 策展 control/health/revision/attempt 的纯展示契约。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

global.window = global;
vm.runInThisContext(fs.readFileSync(require.resolve("./context-curator-presentation.js"), "utf8"));
const view = global.FocusContextCuratorPresentation;

const caughtUp = {
  control_state: "following",
  health_state: "idle",
  observed_checkpoint_id: "cp-2",
  prepared_checkpoint_id: "cp-2",
  published_checkpoint_id: "cp-2",
  latest_revision: { status: "published" },
};
assert.equal(view.isCaughtUp(caughtUp), true);
assert.match(view.followingMessage(caughtUp), /等待根 Context/);
for (const change of [
  { health_state: "preparing" },
  { prepared_checkpoint_id: "cp-1" },
  { published_checkpoint_id: "cp-1" },
  { latest_revision: { status: "publishing" } },
]) {
  const state = { ...caughtUp, ...change };
  assert.equal(view.isCaughtUp(state), false);
  assert.match(view.followingMessage(state), /已观察到新版本/);
}

const cases = [
  [{ ...caughtUp, health_state: "preparing", latest_revision: { status: "preparing", error: "projection" } }, "pending", "正在准备"],
  [{ ...caughtUp, health_state: "degraded", latest_revision: { status: "error", attempts: [{ error_kind: "projection" }] } }, "error", "处理失败"],
  [{ ...caughtUp, health_state: "blocked", latest_revision: { status: "error", attempts: [{ error_kind: "capability" }] } }, "error", "处理阻塞"],
  [{ ...caughtUp, health_state: "blocked", latest_revision: { status: "approval_required" } }, "error", "等待决断"],
  [{ ...caughtUp, control_state: "paused", health_state: "running" }, "interrupted", "已暂停"],
  [{ ...caughtUp, control_state: "stopped" }, "interrupted", "已停止"],
];
for (const [agent, status, label] of cases) {
  assert.equal(view.presentationStatus(agent), status);
  assert.equal(view.stateLabel(agent), label);
}

const attempts = view.attemptRows({ attempts: [
  { attempt_number: 1, model_name: "deepseek", output_method: "prompt_json", status: "error", error_kind: "provider", error: "timeout" },
  { attempt_number: 2, model_name: "deepseek", output_method: "prompt_json", status: "error", error_kind: "publication", error: "checkpoint write failed" },
] });
assert.equal(attempts.length, 2);
assert.equal(attempts[0].error_text, "provider: timeout");
assert.equal(attempts[1].error_text, "publication: checkpoint write failed");

assert.deepEqual(view.messageSemantics({ role: "system" }), {
  kind: "system", roleLabel: "System", associations: [],
});
assert.deepEqual(view.messageSemantics({
  role: "ai",
  tool_calls: [{ id: "call-search", name: "web_search", args: { query: "Focus" } }],
}), {
  kind: "ai", roleLabel: "AI",
  associations: [{ direction: "调用", name: "web_search", tool_call_id: "call-search" }],
});
assert.deepEqual(view.messageSemantics({
  role: "tool", name: "web_search", tool_call_id: "call-search",
}), {
  kind: "tool", roleLabel: "Tool",
  associations: [{ direction: "响应", name: "web_search", tool_call_id: "call-search" }],
});
assert.deepEqual(view.revisionPlanItemTypes({ attempts: [
  { parsed_response: { items: [{ type: "compose_message" }, { type: "tool_exchange" }] } },
  { parsed_response: { items: [{ type: "compose_message" }] } },
] }), ["compose_message", "tool_exchange"]);

console.log("context curator presentation checks passed");
