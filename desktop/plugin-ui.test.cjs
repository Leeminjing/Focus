/*
 * 本文件验证 F20 插件中心纯渲染结果。输入为 active/disabled/unavailable/rejected 插件、接口表和轨迹，
 * 输出为状态筛选、主从详情、错误优先、稳定接口顺序、启停入口可见性、HTML 转义与零插件启动断言。
 */
"use strict";

const assert = require("node:assert/strict");
const view = require("./plugin-view.js");

const plugins = [
  { name: "demo", version: "1.0.0", status: "active", injected: ["tool"], requires: [], can_toggle: true },
  { name: "eyes", version: "0.1.0", status: "unavailable", injected: [], requires: ["demo"], missing: ["vision"] },
  { name: "bad<script>", version: "2", status: "rejected", injected: [], requires: [], conflict: { interface: "tool", current: "demo", new: "bad<script>" } },
  { name: "shelved", version: "0.2.0", status: "disabled", injected: [], requires: [], can_toggle: true },
  { name: "shipped-off", version: "0.3.0", status: "disabled", injected: [], requires: [], can_toggle: false },
];
const interfaces = {
  tool: { plugins: ["demo", "eyes"], cardinality: "multiple" },
  "hook.after": { plugins: ["demo"], read_only: true, cardinality: "single" },
};
const traces = [
  { interface: "tool", plugin: "demo", status: "success", duration_ms: 2 },
  { interface: "tool", plugin: "demo", status: "failed", duration_ms: 4, error: "boom<script>" },
];

const all = view.render(plugins, interfaces, traces, { filter: "all", selectedName: "demo" });
assert.match(all, /按插件状态筛选/);
assert.match(all, /5\/5/);
assert.match(all, /aria-pressed="true"/);
assert.ok(all.indexOf('class="trace-failed"') < all.indexOf('class="trace-success"'), "异常轨迹必须排在成功轨迹之前");
assert.match(all, /Read-only extension point/);
assert.doesNotMatch(all, /bad<script>/);
assert.match(all, /bad&lt;script&gt;/);

const rejected = view.render(plugins, interfaces, traces, { filter: "rejected", selectedName: "bad<script>" });
assert.match(rejected, /1\/5/);
assert.match(rejected, /接口注入冲突/);

// 启停入口：生效插件给「停用」，用户停用的给「启用」，发布方关闭的不给按钮
assert.match(all, /data-action="toggle-plugin" data-plugin-name="demo" data-plugin-enabled="false"/);
assert.match(all, /data-plugin-name="shelved" data-plugin-enabled="true"/);
assert.doesNotMatch(all, /data-plugin-name="shipped-off" data-action="toggle-plugin"/);
assert.doesNotMatch(all, /data-plugin-name="shipped-off"[^>]*data-action="toggle-plugin"/);
assert.match(all, /已被本机停用，不参与能力注入/);

const disabledOnly = view.render(plugins, interfaces, traces, { filter: "disabled", selectedName: "shelved" });
assert.match(disabledOnly, /2\/5/);
assert.match(disabledOnly, /已被本机停用/);

const empty = view.render([], {}, [], { filter: "all" });
assert.match(empty, /尚无插件/);
assert.match(empty, /宿主仍可独立启动/);
