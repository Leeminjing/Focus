"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

async function refresh(result) {
  const harness = createAppHarness({ statusNode: "record", fetch: true });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.context.fetch = async () => ({ ok: true, status: 200, json: async () => result });
  harness.vm.runInContext(`
    state.activeTaskId = "assembly";
    state.tasks = [{ task_id: "assembly", harness_mode: "assembly", workspace_path: "C:/workspace" }];
    state.details.set("assembly", { messages: [], ui_state: {} });
  `, harness.context);
  const button = { dataset: { action: "refresh-capabilities" }, disabled: false };
  harness.context.refreshEvent = { target: { closest: () => button } };
  await harness.vm.runInContext("handleDocumentClick(refreshEvent)", harness.context);
  assert.equal(button.disabled, false);
  return harness;
}

test("all-invalid refresh shows name, scope, reason and repair in the assembly area", async () => {
  const harness = await refresh({ skills: [], mcp_tool_count: 0, failed_servers: ["broken"], mcp_failures: [{
    server_name: "broken<script>", scope: "workspace", config_path: "C:/workspace/.focus/extensions_config.json",
    stage: "configuration", reason: "缺少 command", action: "补充 command 后重试 MCP",
  }] });
  assert.equal(harness.statusState.danger, true);
  assert.match(harness.statusState.text, /MCP 刷新存在失败/);
  const html = harness.vm.runInContext('renderAssemblyTarget(state.tasks[0])', harness.context);
  for (const text of ["broken&lt;script&gt;", "工作区", "extensions_config.json", "缺少 command", "补充 command 后重试 MCP"]) {
    assert.ok(html.includes(text), text);
  }
  assert.doesNotMatch(html, /broken<script>/);
  assert.match(html, /role="status"/);
  assert.equal(harness.vm.runInContext('renderCapabilityRefreshResult("other")', harness.context), "");
});

test("partial refresh preserves available skill and tool counts with failure details", async () => {
  const harness = await refresh({ skills: [{ name: "skill" }], mcp_tool_count: 2, failed_servers: ["offline"], mcp_failures: [{
    server_name: "offline", scope: "global", config_path: "C:/global/extensions_config.json",
    stage: "connection", reason: "connection refused", action: "确认服务可用后重试",
  }] });
  const html = harness.vm.runInContext('renderCapabilityRefreshResult("assembly")', harness.context);
  assert.match(html, /1 个技能、2 个 MCP 工具/);
  assert.match(html, /全局/);
  assert.match(html, /connection refused/);
  assert.equal(harness.statusState.danger, true);
});

test("broken JSON refresh displays the file, scope and syntax repair location", async () => {
  const configPath = "C:/workspace/.focus/extensions_config.json";
  const harness = await refresh({ skills: [{ name: "skill" }], mcp_tool_count: 0,
    failed_servers: ["extensions_config.json"], mcp_failures: [{
      server_name: "extensions_config.json", scope: "workspace", config_path: configPath,
      stage: "configuration", reason: "JSON 语法错误（第 1 行，第 15 列）：Expecting value",
      action: `修复配置文件 ${configPath}，然后重试 MCP。`,
    }],
  });
  const html = harness.vm.runInContext('renderCapabilityRefreshResult("assembly")', harness.context);
  assert.equal(harness.statusState.danger, true);
  for (const text of [configPath, "工作区", "第 1 行，第 15 列", "修复配置文件", "重试 MCP", "1 个技能、0 个 MCP 工具"]) {
    assert.ok(html.includes(text), text);
  }
});

test("empty configuration remains a successful refresh", async () => {
  const harness = await refresh({ skills: [], mcp_tool_count: 0, failed_servers: [], mcp_failures: [] });
  assert.equal(harness.statusState.danger, false);
  assert.match(harness.statusState.text, /已刷新 0 个技能、0 个 MCP 工具/);
  assert.doesNotMatch(harness.statusState.text, /失败/);
});

test("refresh reports custom tools and explains when they take effect", async () => {
  const harness = await refresh({ skills: [], custom_tool_count: 3, mcp_tool_count: 0,
    failed_servers: [], mcp_failures: [] });
  assert.match(harness.statusState.text, /3 个自定义工具/);
  assert.match(harness.statusState.text, /下次运行生效/);
  const html = harness.vm.runInContext('renderAssemblyTarget(state.tasks[0])', harness.context);
  assert.match(html, /3 个自定义工具/);
  assert.match(html, /刷新技能 \/ 自定义工具 \/ 重试 MCP/);
  assert.match(html, /全局自定义工具/);
  assert.match(html, /正在运行.*保持原快照/);
});

test("successful retry replaces the previous failure panel", async () => {
  const harness = await refresh({ skills: [], mcp_tool_count: 0, failed_servers: ["broken"], mcp_failures: [{
    server_name: "broken", scope: "global", stage: "configuration", reason: "缺少 command", action: "补充 command",
  }] });
  harness.context.fetch = async () => ({ ok: true, status: 200, json: async () => ({
    skills: [], mcp_tool_count: 1, failed_servers: [], mcp_failures: [],
  }) });
  await harness.vm.runInContext("handleDocumentClick(refreshEvent)", harness.context);
  const html = harness.vm.runInContext('renderCapabilityRefreshResult("assembly")', harness.context);
  assert.match(html, /1 个 MCP 工具/);
  assert.doesNotMatch(html, /缺少 command|broken|danger/);
  assert.equal(harness.statusState.danger, false);
});
