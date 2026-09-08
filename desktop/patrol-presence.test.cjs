/*
 * 本文件对外提供会话 Patrol 常驻适配纯逻辑检查。输入为零、单、多真实 Agent 与任务
 * 位置映射，输出为待命身份、主操作、不可变映射和首个真实 Agent 原位接替断言；工作流
 * 在 Node 内加载浏览器模块并验证不依赖 DOM。示例：node desktop/patrol-presence.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

global.window = global;
vm.runInThisContext(fs.readFileSync(require.resolve("./context-curator-presentation.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(require.resolve("./patrol-presence.js"), "utf8"));

const presence = global.FocusPatrolPresence;
const standbyPosition = { x: 0.28, y: 0.62 };
const sourcePositions = { __standby__: standbyPosition };
const empty = presence.compose([], sourcePositions);
assert.equal(empty.avatars.length, 1);
assert.deepEqual(empty.avatars[0], {
  avatar_id: "__standby__",
  agent_id: null,
  presence: "standby",
  label: "Patrol 小兵",
  status: "ready",
  status_label: "待命",
  message: "尚未布置任务，需要时可以安排我出发。",
  actions: [
    { id: "configure", label: "布置任务", tone: "primary" },
    { id: "quick-curate", label: "快捷策展", tone: "secondary" },
  ],
  latest_run: null,
});
assert.notEqual(empty.positions, sourcePositions);
assert.equal(empty.positions.__standby__, standbyPosition);

const agents = [
  { agent_id: "patrol-first-0001", permissions: ["read"], latest_run: { status: "pending" } },
  { agent_id: "patrol-second-0002", permissions: ["read"], latest_run: { status: "success" } },
];
const sourceSnapshot = JSON.stringify(agents);
const active = presence.compose(agents, sourcePositions);
assert.equal(active.avatars.length, 2);
assert.deepEqual(active.avatars.map(item => item.avatar_id), ["patrol-first-0001", "patrol-second-0002"]);
assert.ok(active.avatars.every(item => item.presence === "agent"
  && item.actions[0].id === "details" && item.actions[0].label === "查看详情"));
assert.equal(active.avatars[0].status, "pending");
assert.equal(active.avatars[1].status, "success");
assert.deepEqual(active.positions["patrol-first-0001"], standbyPosition);
assert.equal(active.positions["patrol-second-0002"], undefined);
assert.equal(JSON.stringify(agents), sourceSnapshot);
assert.equal(sourcePositions["patrol-first-0001"], undefined);

const ownPosition = { x: 0.7, y: 0.3 };
const positioned = presence.compose(agents, { __standby__: standbyPosition, "patrol-first-0001": ownPosition });
assert.equal(positioned.positions["patrol-first-0001"], ownPosition);

const taskA = presence.compose([], { __standby__: { x: 0.1, y: 0.2 } });
const taskB = presence.compose([], { __standby__: { x: 0.8, y: 0.7 } });
assert.notDeepEqual(taskA.positions.__standby__, taskB.positions.__standby__);

const curatorBase = {
  agent_id: "curator-0001",
  mode: "context_curator",
  latest_run: { status: "success" },
  health_state: "idle",
  observed_checkpoint_id: "cp-1",
  prepared_checkpoint_id: "cp-1",
  published_checkpoint_id: "cp-1",
};
const curatorStates = {
  following: ["ready", "持续跟踪"],
  paused: ["interrupted", "跟踪已暂停"],
  stopped: ["interrupted", "跟踪已停止"],
};
for (const [control_state, [status, status_label]] of Object.entries(curatorStates)) {
  const avatar = presence.agentAvatar({ ...curatorBase, control_state });
  assert.equal(avatar.avatar_id, curatorBase.agent_id);
  assert.equal(avatar.status, status);
  assert.equal(avatar.status_label, status_label);
  assert.match(avatar.label, /^Context 策展/);
}
const updating = presence.agentAvatar({
  ...curatorBase,
  control_state: "following",
  health_state: "running",
  latest_run: { status: "running" },
});
assert.equal(updating.status, "running");
assert.equal(updating.status_label, "正在更新 Context");
const blocked = presence.agentAvatar({
  ...curatorBase,
  control_state: "following",
  health_state: "blocked",
});
assert.equal(blocked.status, "error");
assert.equal(blocked.status_label, "处理阻塞");
const preparing = presence.agentAvatar({
  ...curatorBase,
  control_state: "following",
  health_state: "preparing",
  observed_checkpoint_id: "cp-2",
});
assert.equal(preparing.status_label, "正在准备来源");

console.log("patrol presence checks passed");
