/*
 * 本文件验证准入待决面板：字段齐备、目标按操作类型分行、三个动作齐备、风险确认覆盖两点、
 * 后台待决不抢占主流程面板、会话视图不可用时待决仍可见，以及中英两种语言下新增文案齐备。
 *
 * 输入为中断载荷、面板模板与界面文案；输出为纯逻辑断言、静态标记断言与一次 VM 内的面板装配结果。
 * 工作流：先验纯逻辑（字段 / 目标分行 / 动作 / 恢复值）与标记文案齐备，再在 harness 内装配面板、
 * 点击三个动作，并覆盖「会话视图不可用」与「回到会话视图后恢复挂载」两条可见性路径。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const approval = require("./access-approval.js");

const PAYLOAD = {
  type: "access_review",
  tool: "write_file",
  access_mode: "workspace",
  cwd: "C:/workspace",
  agent_role: "main",
  reads: ["C:/workspace/input.md"],
  writes: ["C:/outside/notes.txt"],
};
const COMMAND_PAYLOAD = {
  type: "access_review",
  tool: "powershell",
  access_mode: "workspace",
  cwd: "C:/workspace",
  agent_role: "main",
  reads: [],
  writes: [],
  command: "Remove-Item ../x",
};

function readSource(name) {
  return fs.readFileSync(path.join(__dirname, name), "utf8");
}

// 1) 字段齐备：工具、目标或命令、执行位置、发起角色、当前访问模式
const fields = approval.approvalFields(PAYLOAD);
assert.deepEqual(
  fields.map(field => field.key),
  ["tool", "target", "cwd", "agent_role", "access_mode"],
  "面板必须呈现五个字段",
);
assert.equal(fields.find(field => field.key === "tool").value, "write_file");
assert.equal(fields.find(field => field.key === "cwd").value, "C:/workspace");
assert.equal(fields.find(field => field.key === "agent_role").value, "main");
assert.equal(fields.find(field => field.key === "access_mode").valueKey, "access.mode_workspace");
assert.equal(
  approval.approvalFields({ type: "access_review", tool: "read_file" })
    .find(field => field.key === "cwd").value,
  "",
  "载荷缺失即留空，不填占位值",
);

// 2) 目标按操作类型分行：同一路径可能被读、被写或两者兼有，人据此判断会不会被改写
const lines = approval.targetLines(PAYLOAD);
assert.deepEqual(
  lines.map(line => [line.operation, line.value]),
  [["read", "C:/workspace/input.md"], ["write", "C:/outside/notes.txt"]],
);
assert.equal(lines[0].operationKey, "access.operation_read");
assert.equal(lines[1].operationKey, "access.operation_write");
assert.deepEqual(
  approval.targetLines({ type: "access_review", reads: ["x"], writes: ["x"] })
    .map(line => line.operation),
  ["read", "write"],
  "同一路径同时被读与被写时两行都保留",
);
assert.deepEqual(
  approval.targetLines(COMMAND_PAYLOAD).map(line => [line.operation, line.value]),
  [["command", "Remove-Item ../x"]],
  "有命令时只呈现命令，并标注为执行",
);

// 3) 动作齐备与恢复值：恢复值只回答「这一次」，放宽动作由调用方另行持久化
assert.deepEqual([...approval.ACTIONS], ["approve_once", "reject", "switch_full"]);
assert.deepEqual(approval.resumeValue("approve_once"), { decision: "approve" });
assert.deepEqual(approval.resumeValue("switch_full"), { decision: "approve" });
assert.deepEqual(approval.resumeValue("reject"), { decision: "reject" });
assert.equal(approval.widensAccess("switch_full"), true);
assert.equal(approval.widensAccess("approve_once"), false);

// 4) 主执行身份与后台执行主体的分流
assert.equal(approval.isAccessReview(PAYLOAD), true);
assert.equal(approval.isAccessReview({ type: "commitment_review" }), false);
assert.equal(approval.isMainSubject("main:task-1"), true);
assert.equal(approval.isMainSubject("swarm:agent-1"), false);

// 5) 面板标记齐备：三个动作按钮、字段容器与风险确认段落都在模板里
const index = readSource("index.html");
assert.match(index, /<template id="accessReviewTemplate">/);
for (const action of approval.ACTIONS) {
  assert.ok(index.includes(`data-access-action="${action}"`), `模板必须提供动作按钮 ${action}`);
}
assert.match(index, /class="access-review-risk"[^>]*hidden/);
assert.match(index, /class="access-review-fields"/);
assert.match(index, /id="accessPending"/);

// 6) 风险确认覆盖两点：不绕过操作系统自身权限、不关闭其他人工审核机制；两种语言齐备
const i18n = readSource("i18n.js");
const [zh, en] = i18n.split('"en-US": Object.freeze({');
assert.ok(zh && en, "两种语言都在文案表里");
assert.match(zh, /"access\.risk_os_permissions": "[^"]*操作系统/);
assert.match(zh, /"access\.risk_other_reviews": "[^"]*其他人工审核机制/);
assert.match(en, /"access\.risk_os_permissions": "[^"]*operating system/);
assert.match(en, /"access\.risk_other_reviews": "[^"]*other human review/);
for (const key of [
  ...approval.RISK_NOTICE_KEYS,
  ...Object.values(approval.OPERATION_KEYS),
  "access.subject_main",
  "access.badge",
]) {
  assert.ok(zh.includes(`"${key}"`) && en.includes(`"${key}"`), `${key} 两种语言齐备`);
}

// 7) 面板装配：主执行身份落在主流程面板，后台执行主体落在后台待处理区
function nodeStub(name) {
  const listeners = new Map();
  const nodes = new Map();
  const children = [];
  const node = {
    name,
    parent: null,
    dataset: {},
    hidden: false,
    isConnected: true,
    innerHTML: "",
    textContent: "",
    children,
    actions: [],
    classList: { add() {}, toggle() {} },
    detach(item) {
      const index = children.indexOf(item);
      if (index >= 0) children.splice(index, 1);
    },
    append(...items) {
      for (const item of items) {
        item.parent?.detach(item);
        item.parent = node;
        children.push(item);
      }
      return node;
    },
    remove() {
      node.parent?.detach(node);
      node.parent = null;
    },
    addEventListener(type, handler) { listeners.set(type, handler); },
    click() { return listeners.get("click")?.(); },
    querySelector(selector) {
      if (selector === ".access-review-panel") {
        return children.find(child => child.name === "panel") || null;
      }
      if (!nodes.has(selector)) nodes.set(selector, nodeStub(selector));
      return nodes.get(selector);
    },
    querySelectorAll(selector) {
      return selector === "[data-access-action]" ? node.actions : [];
    },
  };
  return node;
}

const panel = nodeStub("panel");
panel.actions = approval.ACTIONS.map(action => {
  const button = nodeStub(action);
  button.dataset.accessAction = action;
  return button;
});
const actionButton = action => panel.actions.find(button => button.dataset.accessAction === action);

const template = { content: { cloneNode: () => ({ querySelector: () => panel }) } };
const conversation = nodeStub("conversation");
const accessPending = nodeStub("accessPending");
const fetches = [];
let conversationAvailable = true;

const harness = createAppHarness({
  statusNode: "record",
  fetch: true,
  selectors: {
    "#conversation": () => (conversationAvailable ? conversation : null),
    "#accessPending": accessPending,
    "#accessReviewTemplate": template,
  },
});
harness.context.fetch = async (url, options = {}) => {
  fetches.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ run_id: "run-access", status: "pending" }) };
};
new vm.Script(readAppSource()).runInContext(harness.context);

const seedTask = () => new vm.Script(`
  state.tasks = [{ task_id: "task", thread_id: "thread", workspace_id: "ws", workspace_path: "C:/workspace" }];
  state.activeTaskId = "task";
  state.details.set("task", { messages: [], ui_state: {}, active_run: { run_id: "run-access" } });
  state.accessReviews = { panel: null, payload: null, taskId: null, key: null, busy: false };
  clearStreamBuffer("run-access");
`).runInContext(harness.context);

const open = (agentId, payload = PAYLOAD) => new vm.Script(`
  showAccessReview(state.tasks[0], ${JSON.stringify(payload)}, ${JSON.stringify(agentId)});
`).runInContext(harness.context);

const flush = async () => {
  for (let tick = 0; tick < 6; tick += 1) await new Promise(resolve => setImmediate(resolve));
};

(async () => {
  seedTask();
  open("main:task");
  assert.equal(conversation.children.length, 1, "主执行身份的待决进入主流程面板");
  assert.equal(accessPending.children.length, 0, "主执行身份的待决不进入后台待处理区");
  assert.equal(panel.dataset.subject, "main");
  const fieldsHtml = panel.querySelector(".access-review-fields").innerHTML;
  assert.ok(fieldsHtml.includes("write_file"), "面板呈现工具名");
  assert.ok(fieldsHtml.includes("C:/outside/notes.txt"), "面板呈现规范化真实目标");
  assert.ok(fieldsHtml.includes("写入"), "目标行标注操作类型");
  assert.equal(panel.querySelector(".switch-full-button").hidden, false, "主执行身份提供放宽动作");

  // 7a) 会话视图不可用时，主执行身份的待决退回始终存在的待处理区（不静默丢失）
  conversationAvailable = false;
  conversation.children.length = 0;
  seedTask();
  open("main:task");
  assert.equal(conversation.children.length, 0, "会话视图不可用时不再挂到会话区");
  assert.equal(accessPending.children.length, 1, "主执行身份的待决仍可见");
  assert.equal(accessPending.hidden, false, "待处理区被展开");

  // 7b) 回到会话视图后恢复挂载到会话区，并收起空的待处理区
  conversationAvailable = true;
  new vm.Script("restoreAccessReviewPanels(document.querySelector('#conversation'));")
    .runInContext(harness.context);
  assert.equal(conversation.children.length, 1, "回到会话视图后待决移回会话区");
  assert.equal(accessPending.children.length, 0, "待处理区不再保留该面板");
  assert.equal(accessPending.hidden, true, "待处理区收起");

  // 7c) 放宽范围先出确认，未确认不提交
  actionButton("switch_full").click();
  assert.equal(panel.querySelector(".access-review-risk").hidden, false, "点击放宽动作先显示风险确认");
  assert.equal(fetches.length, 0, "未确认前不发起恢复请求");
  panel.querySelector(".cancel-full-button").click();
  assert.equal(panel.querySelector(".access-review-risk").hidden, true, "取消确认后收起风险段落");

  // 7d) 「仅允许这一次」→ 只提交 approve
  actionButton("approve_once").click();
  await flush();
  assert.equal(fetches.length, 1, "允许这一次发起一次恢复请求");
  assert.ok(fetches[0].url.endsWith("/desktop/api/threads/thread/runs/resume"));
  assert.deepEqual(JSON.parse(fetches[0].options.body), { resume: { decision: "approve" } });
  assert.match(harness.statusNode.textContent, /已允许这一次/);

  // 7e) 确认放宽 → 提交 approve 并把后续运行切到完全权限
  seedTask();
  fetches.length = 0;
  open("main:task");
  actionButton("switch_full").click();
  panel.querySelector(".confirm-full-button").click();
  await flush();
  assert.deepEqual(JSON.parse(fetches[0].options.body), { resume: { decision: "approve" } });
  assert.match(harness.statusNode.textContent, /后续运行按完全权限执行/);
  assert.equal(
    new vm.Script(`state.details.get("task").ui_state.access_mode`).runInContext(harness.context),
    "full",
    "放宽选择持久化到任务 ui_state",
  );

  // 7f) 拒绝 → 提交 reject 并给出可读结果
  seedTask();
  fetches.length = 0;
  open("main:task");
  actionButton("reject").click();
  await flush();
  assert.deepEqual(JSON.parse(fetches[0].options.body), { resume: { decision: "reject" } });
  assert.match(harness.statusNode.textContent, /已拒绝/);
  assert.match(harness.statusNode.textContent, /write_file/);

  // 7g) 后台执行主体的待决：可见、可处理、不进入主流程面板
  seedTask();
  fetches.length = 0;
  conversation.children.length = 0;
  accessPending.children.length = 0;
  open("swarm:agent-1");
  assert.equal(conversation.children.length, 0, "后台待决不进入主流程面板");
  assert.equal(accessPending.children.length, 1, "后台待决在后台待处理区可见");
  assert.equal(accessPending.hidden, false);
  assert.equal(panel.dataset.subject, "background");
  assert.equal(panel.querySelector(".switch-full-button").hidden, true, "后台执行主体的放宽动作不开放");
  actionButton("approve_once").click();
  await flush();
  assert.equal(fetches.length, 1, "后台待决可处理");
  assert.deepEqual(JSON.parse(fetches[0].options.body), { resume: { decision: "approve" } });

  console.log("access-approval.test.cjs OK");
})().catch(error => { console.error(error); process.exit(1); });
