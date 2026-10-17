/* 验证恢复清单、HTML 转义、核验请求和失败后保留受阻 Run。 */
"use strict";
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource, createNode } = require("./test-helper.cjs");

async function main() {
  const host = createNode();
  const feedback = createNode();
  const dialog = createNode({ querySelector: selector => selector === "#shellRecoveryHost" ? host : selector === "#shellRecoveryFeedback" ? feedback : null });
  const calls = [];
  const runs = [{ run_id: "blocked-run", title: "<script>unsafe</script>", status: "interrupted",
    directory: "C:/work/<folder>", attempts: [{ call_id: "shell-call", tool: "powershell", state: "unknown" }] }];
  const harness = createAppHarness({ selectors: { "#settingsDialog": dialog }, statusNode: "record",
    globals: { recoveryCalls: calls, recoveryRuns: runs } });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`api = async (path, options = {}) => {
    recoveryCalls.push({ path, options });
    if (options.method === "POST") return { status: "interrupted" };
    return recoveryRuns;
  };`, harness.context);
  const html = harness.vm.runInContext("renderShellRecovery(recoveryRuns)", harness.context);
  assert.ok(html.includes("blocked-run") && html.includes("shell-call"));
  assert.ok(html.includes("C:/work/&lt;folder&gt;") && !html.includes("<script>"));
  assert.ok(html.includes("重启 Windows") && html.includes("不会自动重放"));
  assert.ok(harness.vm.runInContext("renderShellRecovery([])", harness.context).includes("没有等待"));
  await harness.vm.runInContext("loadShellRecovery()", harness.context);
  assert.ok(host.innerHTML.includes("blocked-run"));
  const handler = harness.listeners.get("click").find(fn => fn.toString().includes("recover-shell-cleanup"));
  const button = { dataset: { action: "recover-shell-cleanup", runId: "blocked-run" }, disabled: false };
  handler({ target: { closest: () => button } });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.some(call => call.path === "/desktop/api/runs/blocked-run/shell-recovery" && call.options.method === "POST"));
  assert.equal(button.disabled, false);
  assert.ok(harness.statusState.text.includes("目录已释放"));
  assert.ok(feedback.textContent.includes("目录已释放"));
  harness.vm.runInContext(`api = async (path, options = {}) => {
    if (options.method === "POST") throw new Error("尚无匹配的退出证据");
    return recoveryRuns;
  };`, harness.context);
  handler({ target: { closest: () => button } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(button.disabled, false);
  assert.ok(host.innerHTML.includes("blocked-run"));
  assert.ok(harness.statusState.text.includes("退出证据") && harness.statusState.danger);
  assert.ok(feedback.textContent.includes("退出证据"));
  console.log("Shell recovery UI passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
