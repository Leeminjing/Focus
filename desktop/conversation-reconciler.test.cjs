/*
 * 本文件验证对话增量对账纯函数。输入为带稳定 key 的渲染单元序列，输出为按序编辑指令；
 * 工作流覆盖全等、纯追加、纯移除、原位更新、key 兜底与异常输入，不涉及 DOM。
 */
"use strict";

const assert = require("node:assert/strict");
const { unitKey, diffUnits } = require("./conversation-reconciler.js");

function msg(key, signature) {
  return { kind: "message", key, signature: signature || `body-${key}` };
}
function evt(key) {
  return { kind: "event", key, signature: `event-${key}` };
}
function div(key) {
  return { kind: "divider", key, signature: `divider-${key}` };
}

function opsOf(prev, next) {
  return diffUnits(prev, next).map(p => p.op);
}

// 全等：全部 keep，无 append/update/remove
{
  const prev = [msg("a"), evt("t1"), msg("b")];
  const next = [msg("a"), evt("t1"), msg("b")];
  const ops = diffUnits(prev, next);
  assert.deepEqual(ops.map(o => o.op), ["keep", "keep", "keep"], "全等序列只产生 keep 指令");
  assert.deepEqual(ops.map(o => o.prevIndex), [0, 1, 2]);
  assert.deepEqual(ops.map(o => o.nextIndex), [0, 1, 2]);
}

// 纯追加：尾部新增消息
{
  const prev = [msg("a"), msg("b")];
  const next = [msg("a"), msg("b"), msg("c"), msg("d")];
  const ops = diffUnits(prev, next);
  assert.deepEqual(ops.filter(o => o.op === "keep").map(o => o.prevIndex), [0, 1]);
  assert.deepEqual(ops.filter(o => o.op === "append").map(o => o.nextIndex), [2, 3]);
  assert.equal(ops.some(o => o.op === "remove"), false);
}

// 纯移除：尾部消息消失
{
  const prev = [msg("a"), msg("b"), msg("c")];
  const next = [msg("a"), msg("b")];
  const ops = diffUnits(prev, next);
  assert.deepEqual(ops.filter(o => o.op === "keep").length, 2);
  assert.deepEqual(ops.filter(o => o.op === "remove").map(o => o.prevIndex), [2]);
}

// 原位更新：同 key 签名变化
{
  const prev = [msg("a"), msg("b", "old-signature")];
  const next = [msg("a"), msg("b", "new-signature")];
  const ops = diffUnits(prev, next);
  const update = ops.filter(o => o.op === "update");
  assert.equal(update.length, 1);
  assert.equal(update[0].prevIndex, 1);
  assert.equal(update[0].nextIndex, 1);
}

// 中部插入 + 中部移除：压缩 undo 场景
{
  const prev = [msg("a"), msg("b"), msg("c")];
  const next = [msg("a"), msg("x"), msg("c")];
  const ops = diffUnits(prev, next);
  assert.deepEqual(ops.filter(o => o.op === "append").map(o => o.nextIndex), [1]);
  assert.deepEqual(ops.filter(o => o.op === "remove").map(o => o.prevIndex), [1]);
  assert.deepEqual(ops.filter(o => o.op === "keep").map(o => o.prevIndex), [0, 2]);
}

// 事件与分界混合
{
  const prev = [msg("a"), div("d1"), msg("b")];
  const next = [msg("a"), div("d1"), msg("b"), evt("t9")];
  const ops = diffUnits(prev, next);
  assert.deepEqual(ops.filter(o => o.op === "append").map(o => o.nextIndex), [3]);
  assert.deepEqual(ops.filter(o => o.op === "keep").length, 3);
}

// key 兜底：unitKey 对空/无 key 单元返回 null
{
  assert.equal(unitKey(null), null);
  assert.equal(unitKey({}), null);
  assert.equal(unitKey({ key: "" }), null);
  assert.equal(unitKey({ key: "x" }), "x");
}

// 无 key 的 next 单元按 append 处理，不抛异常
{
  const prev = [msg("a")];
  const next = [msg("a"), { kind: "message", key: null }];
  const ops = diffUnits(prev, next);
  assert.equal(ops.filter(o => o.op === "append").length, 1);
}

// 异常输入：非数组按空数组处理，返回空指令
{
  assert.deepEqual(diffUnits(null, null), []);
  assert.deepEqual(diffUnits([msg("a")], null), [{ op: "remove", prevIndex: 0 }]);
  assert.deepEqual(diffUnits(null, [msg("a")]), [{ op: "append", nextIndex: 0 }]);
}

console.log("conversation-reconciler: 全等、增删改、事件分界与异常输入对账通过");
