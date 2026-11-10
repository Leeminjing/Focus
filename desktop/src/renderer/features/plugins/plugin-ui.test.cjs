/*
 * 本文件验证 F20 插件中心纯渲染结果。输入为 active/unavailable/rejected 插件、接口表和轨迹，
 * 输出为状态筛选、主从详情、错误优先、稳定接口顺序、HTML 转义与零插件启动断言。
 */
"use strict";

const assert = require("node:assert/strict");
const view = require("./plugin-view.js");

const plugins = [
  { name: "demo", version: "1.0.0", status: "active", injected: ["tool"], requires: [] },
  { name: "eyes", version: "0.1.0", status: "unavailable", injected: [], requires: ["demo"], missing: ["vision"] },
  { name: "bad<script>", version: "2", status: "rejected", injected: [], requires: [], conflict: { interface: "tool", current: "demo", new: "bad<script>" } },
];
const interfaces = {
  tool: { plugins: ["demo", "eyes"], cardinality: "multiple" },
  "hook.after": { plugins: ["demo"], read_only: true, cardinality: "single" },
};
const traces = [
  { interface: "tool", plugin: "demo", status: "success", duration_ms: 2 },
  { interface: "tool", plugin: "demo", status: "failed", duration_ms: 4, error: "boom<script>" },
];

const activation = { registry: "immediate", tools_and_hooks: "next_run", running_runs: "unchanged", http_routes: "process_restart", desktop_assets: "process_restart" };
const all = view.render(plugins, interfaces, traces, { filter: "all", selectedName: "demo", activation });
assert.match(all, /按插件状态筛选/);
assert.match(all, /已注册/);
assert.doesNotMatch(all, /已生效/);
assert.match(all, />刷新状态<\/button>/);
assert.match(all, />加载插件变更<\/button>/);
assert.doesNotMatch(all, /重新加载注册表|PLUGIN DETAIL|plugin-detail-heading/);
assert.match(all, /下次 Run 装配时生效/);
assert.match(all, /正在运行的 Run 保留原快照/);
assert.match(all, /data-plugin-activation="desktop_assets"[^]*?重启后端进程后生效/);
assert.match(all, /data-plugin-activation="http_routes"[^]*?重启后端进程后生效/);
assert.match(all, /退出并重新启动 Focus/);
assert.match(all, /仅读取当前状态/);
assert.match(all, /注册状态：/);
assert.doesNotMatch(view.renderPluginDetail(plugins[0], interfaces, traces, activation), /已注册|PLUGIN DETAIL|<h2/);
assert.match(all, /3\/3/);
assert.match(all, /aria-pressed="true"/);
assert.ok(all.indexOf('class="trace-failed"') < all.indexOf('class="trace-success"'), "异常轨迹必须排在成功轨迹之前");
assert.match(all, /Read-only extension point/);
assert.doesNotMatch(all, /bad<script>/);
assert.match(all, /bad&lt;script&gt;/);

const rejected = view.render(plugins, interfaces, traces, { filter: "rejected", selectedName: "bad<script>" });
assert.match(rejected, /1\/3/);
assert.match(rejected, /接口注入冲突/);

const empty = view.render([], {}, [], { filter: "all" });
assert.match(empty, /尚无插件/);
assert.match(empty, /宿主仍可独立启动/);

// A registration badge cannot supply missing activation evidence.
const unknown = view.renderActivation({ registry: "immediate", tools_and_hooks: "future-policy" });
assert.match(unknown, /生效时机未知/);
assert.match(unknown, /运行中 Run 的生效范围未知/);
assert.doesNotMatch(unknown, /下次 Run 装配时生效|正在运行的 Run 保留原快照/);

const loadedButUnread = view.render(plugins, interfaces, traces, {
  activation, reloadSucceeded: true, error: "query failed<script>",
});
assert.match(loadedButUnread, /已加载到注册表/);
assert.match(loadedButUnread, /状态查询未完成/);
assert.match(loadedButUnread, /刷新状态失败：query failed&lt;script&gt;/);
assert.doesNotMatch(loadedButUnread, /加载结果未确认/);
