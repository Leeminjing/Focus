/*
 * 本文件验证 F20 运行中心的展示契约。输入为宿主渲染源码、模板和覆盖层样式，输出为状态词汇、
 * 九阶段摘要、固定审批区、恢复语义及压缩顺序布局断言；工作流只检查展示映射，不改写后端状态。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const appSource = readAppSource();
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "styles", "views.css"), "utf8");

const { context } = createAppHarness();
new vm.Script(appSource).runInContext(context);

const statuses = new vm.Script(`
  ["pending", "running", "success", "interrupted", "error", "cancelled", "ready"]
    .map(status => [status, presentRunStatus(status)])
`).runInContext(context);

assert.deepEqual(JSON.parse(JSON.stringify(statuses)), [
  ["pending", { label: "排队中", tone: "active" }],
  ["running", { label: "运行中", tone: "active" }],
  ["success", { label: "已完成", tone: "success" }],
  ["interrupted", { label: "已中断", tone: "warning" }],
  ["error", { label: "运行失败", tone: "danger" }],
  ["cancelled", { label: "已取消", tone: "warning" }],
  ["ready", { label: "就绪", tone: "neutral" }],
]);
assert.match(appSource, /Object\.freeze\(\{[\s\S]*pending:[\s\S]*cancelled:/);
assert.match(appSource, /for \(let number = 1; number <= 9; number \+= 1\)/);
assert.match(appSource, /container\.dataset\.stage = String\(stage \|\| 0\)/);

assert.match(html, /<template id="traceTemplate">[\s\S]*trace-elapsed[\s\S]*trace-count/);
assert.match(html, /<template id="reviewTemplate">[\s\S]*approve-button[\s\S]*revision-form/);
assert.match(appSource, /data-recovery-status|dataset\.recoveryStatus = "resumable"/);
assert.match(appSource, /dataset\.recoveryStatus = "processing"/);
assert.match(appSource, /dataset\.recoveryStatus = "orphaned"/);
assert.match(css, /\.review-actions,[\s\S]*position: sticky;[\s\S]*bottom: 0/);

assert.match(appSource, /class="compression-panels"/);
assert.match(appSource, /01 · SOURCE/);
assert.match(appSource, /02 · PLAN/);
assert.match(appSource, /确认前不写入/);
assert.match(appSource, /只有确认后才会写入 checkpoint/);
assert.match(css, /\.compression-panels \{[\s\S]*grid-template-columns: minmax\(300px, 5fr\) minmax\(420px, 7fr\)/);
assert.match(css, /@media \(max-width: 900px\)[\s\S]*\.compression-panels \{ min-height: auto; grid-template-columns: 1fr; \}/);

