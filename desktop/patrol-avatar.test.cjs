/*
 * 本文件对外提供会话 Patrol 小兵纯逻辑检查。输入为状态、位置、画布尺寸和移动增量，
 * 输出为资源映射、归一化坐标与拖动判定断言；工作流在 Node 内加载浏览器模块并验证
 * 不依赖 DOM 的边界。示例：node desktop/patrol-avatar.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

global.window = global;
vm.runInThisContext(fs.readFileSync(require.resolve("./patrol-avatar.js"), "utf8"));

const avatar = global.FocusPatrolAvatar;
assert.equal(avatar.normalizeStatus("running"), "running");
assert.equal(avatar.normalizeStatus("unknown"), "ready");
assert.deepEqual(
  ["ready", "pending", "running", "success", "interrupted", "cancelled", "error"].map(status => avatar.statusPresentation(status).asset),
  ["idle", "loading", "working", "happy", "thinking", "thinking", "alert"],
);

assert.deepEqual(avatar.sanitizePosition({ x: -3, y: 4 }), { x: 0, y: 1 });
assert.deepEqual(avatar.sanitizePosition({ x: "bad", y: null }, { x: 0.2, y: 0.4 }), { x: 0.2, y: 0 });
const defaults = Array.from({ length: 10 }, (_item, index) => avatar.defaultPosition(index));
assert.equal(new Set(defaults.map(value => `${value.x}:${value.y}`)).size, defaults.length);
assert.ok(defaults.every(value => value.x >= 0 && value.x <= 1 && value.y >= 0 && value.y <= 1));

const bounds = { width: 1000, height: 700 };
const size = { width: 100, height: 100 };
const pixels = avatar.pixelPosition({ x: 0.25, y: 0.75 }, bounds, size);
assert.deepEqual(pixels, { left: 225, top: 450 });
assert.deepEqual(avatar.normalizedPosition(pixels.left, pixels.top, bounds, size), { x: 0.25, y: 0.75 });
assert.deepEqual(avatar.normalizedPosition(9999, -2, bounds, size), { x: 1, y: 0 });

assert.equal(avatar.dragExceeded(0, 0, 3, 3), false);
assert.equal(avatar.dragExceeded(0, 0, 4, 4), true);
assert.equal(avatar.movementState(-8, 1), "move-left");
assert.equal(avatar.movementState(8, 1), "move-right");
assert.equal(avatar.movementState(1, -8), "rise");
assert.equal(avatar.movementState(1, 8), "descend");
assert.equal(avatar.movementState(8, 8), "rotate");
assert.equal(avatar.movementState(0, 0), null);
assert.deepEqual(avatar.stableMovementState(0, 0, 4, 3, "move-right"), { motion: "move-right", sampled: false });
assert.deepEqual(avatar.stableMovementState(0, 0, 13, 0, "rise"), { motion: "move-right", sampled: true });
assert.deepEqual(avatar.stableMovementState(0, 0, 0, -13, "move-right"), { motion: "rise", sampled: true });
assert.deepEqual(avatar.stableMovementState(0, 0, 10, 8, "move-right"), { motion: "rotate", sampled: true });
assert.deepEqual(avatar.stableMovementState(0, 0, 13, 7, "rotate"), { motion: "rotate", sampled: true });
assert.deepEqual(avatar.stableMovementState(0, 0, 13, 7, "move-right"), { motion: "move-right", sampled: true });
assert.deepEqual(avatar.stableMovementState(0, 0, 13, 5, "rotate"), { motion: "move-right", sampled: true });
assert.equal(avatar.avatarId({ avatar_id: "__standby__", agent_id: null }), "__standby__");
assert.equal(avatar.avatarId({ agent_id: "patrol-123456789" }), "patrol-123456789");
assert.equal(avatar.avatarStatus({ status: "ready", latest_run: { status: "running" } }), "ready");
assert.equal(avatar.avatarStatus({ latest_run: { status: "running" } }), "running");
assert.equal(avatar.avatarLabel({ label: "Patrol 小兵" }), "Patrol 小兵");
assert.equal(avatar.avatarLabel({ agent_id: "patrol-123456789" }), "小兵 patrol-1");

console.log("patrol avatar checks passed");
