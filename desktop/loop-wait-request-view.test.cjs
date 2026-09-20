/**
 * 本文件对外提供 Loop WaitRequest 渲染与草稿隔离回归测试。
 * 输入为全部声明的 response mode、来源/范围和模拟 localStorage；输出为绑定 request id 的控件 HTML 和跨实例逐请求草稿。
 * 具体工作流为渲染各响应模式、保存草稿、重建 store 并切换请求验证隔离。示例：`node --test desktop/loop-wait-request-view.test.cjs`。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const storage = new Map();
const context = { window: { localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) } } };
vm.createContext(context);
vm.runInContext(readFileSync(path.join(__dirname, "loop-wait-request-view.js"), "utf8"), context);
const WaitView = context.window.FocusLoopWaitRequestView;
const LiveSchema = require("./loop-live-schema.js");
const LiveReducer = require("./loop-live-reducer.js");
const LiveSelectors = require("./loop-live-selectors.js");

test("renders text and typed action requests without magic chat text", () => {
  const text = WaitView.render({ wait_request_id: "w1", prompt: "Which target?", response_mode: "text", response_contract: { max_length: 20 }, scope: { kind: "portfolio" } });
  const action = WaitView.render({ wait_request_id: "w2", prompt: "Budget exhausted", response_mode: "action", response_contract: { actions: [{ action: "revise_budget", label: "Revise" }, { action: "stop", label: "Stop" }] }, scope: { kind: "loop" } });
  assert.match(text, /data-wait-request-id="w1"/);
  assert.match(text, /textarea/);
  assert.match(action, /data-wait-action="revise_budget"/);
  assert.match(action, /data-wait-action="stop"/);
});

test("renders choice and structured modes with source, scope and restored values", () => {
  const single = WaitView.render({ request_id: "w3", prompt: "Pick one", created_by: "budget-guard", response_mode: "single_choice", response_contract: { options: [{ value: "a", label: "A" }] }, scope: { context_id: "c1" } }, { draft: JSON.stringify({ choice: "a" }) });
  const multiple = WaitView.render({ request_id: "w4", prompt: "Pick many", response_mode: "multiple_choice", response_contract: { options: [{ value: "a" }, { value: "b" }] } }, { draft: JSON.stringify({ choice: ["a", "b"] }) });
  const structured = WaitView.render({ request_id: "w5", prompt: "Details", response_mode: "structured", response_contract: { fields: { reason: { label: "Reason", required: true } } } }, { draft: JSON.stringify({ reason: "because" }) });
  assert.match(single, /来源：budget-guard/);
  assert.match(single, /context_id/);
  assert.match(single, /value="a" checked/);
  assert.equal((multiple.match(/ checked/g) || []).length, 2);
  assert.match(structured, /value="because"/);
});

test("drafts are isolated by wait request identity", () => {
  const drafts = WaitView.createDraftStore();
  drafts.set("w1", "answer one");
  drafts.set("w2", "answer two");
  assert.equal(drafts.get("w1"), "answer one");
  assert.equal(drafts.get("w2"), "answer two");
  const restored = WaitView.createDraftStore();
  assert.equal(restored.get("w1"), "answer one");
  restored.delete("w1");
  assert.equal(WaitView.createDraftStore().get("w1"), "");
});

test("live wait entities replay every lifecycle state and tolerate future modes", () => {
  let projection = LiveSchema.emptyProjection("loop-1");
  const event = (sequence, id, revision, status, responseMode = "text") => ({
    event_id: `event-${sequence}`,
    loop_id: "loop-1",
    sequence,
    kind: `loop.wait.${status}`,
    entity_type: "loop_wait_request",
    entity_id: id,
    entity_revision: revision,
    payload: { request_id: id, status, response_mode: responseMode },
  });
  projection = LiveReducer.reduce(projection, event(1, "w1", 1, "open"));
  assert.equal(LiveSelectors.selectActiveWaitRequest(projection).request_id, "w1");
  projection = LiveReducer.reduce(projection, event(2, "w1", 2, "resolving"));
  assert.equal(LiveSelectors.selectActiveWaitRequest(projection).status, "resolving");
  projection = LiveReducer.reduce(projection, event(3, "w1", 3, "resolved"));
  assert.equal(LiveSelectors.selectActiveWaitRequest(projection), null);
  projection = LiveReducer.reduce(projection, event(4, "w2", 1, "cancelled"));
  projection = LiveReducer.reduce(projection, event(5, "w3", 1, "superseded"));
  projection = LiveReducer.reduce(projection, event(6, "w4", 1, "open", "future_modal"));
  assert.equal(LiveSelectors.selectActiveWaitRequest(projection).response_mode, "future_modal");
  assert.equal(LiveReducer.reduce(projection, event(6, "w4", 1, "open", "future_modal")), projection);
});
