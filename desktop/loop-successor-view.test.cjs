/**
 * 本文件对外提供后继 Loop 授权视图的结构化门禁回归测试。
 * 输入为最新直接用户 Run 的 eligibility、前置 Loop 与候选状态；输出为明确 Run 身份、阻塞原因和稳定 Mission 表单断言。
 * 具体工作流为直接渲染未启动视图，分别验证可授权、waiting_user 冲突和已绑定最新 Run，确保界面不回退旧候选。
 * 示例：`node --test desktop/loop-successor-view.test.cjs`。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const View = require("./loop-view.js");

test("eligible successor names the selected newest direct-user Run", () => {
  const html = View.render({ snapshot: null }, {
    title: "Context",
    loop_activation: { eligible: true, candidate_run_id: "run-new", candidate_status: "success", consistency_token: "a".repeat(64) },
    latest_direct_user_run: { run_id: "run-new", status: "success", origin: "direct_user" },
  });
  assert.match(html, /run-new/);
  assert.doesNotMatch(html, /button class="primary" type="submit" disabled/);
});

test("nonterminal predecessor and bound newest Run expose structured conflicts", () => {
  const waiting = View.render({ snapshot: null }, {
    title: "Context",
    loop_activation: { eligible: false, candidate_run_id: "run-new", candidate_status: "success", predecessor_loop_id: "loop-old", reason: "nonterminal_predecessor" },
  });
  assert.match(waiting, /data-eligibility-reason="nonterminal_predecessor"/);
  assert.match(waiting, /loop-old/);
  assert.match(waiting, /仍有未结束的 Loop/);
  const bound = View.render({ snapshot: null }, {
    title: "Context",
    loop_activation: { eligible: false, candidate_run_id: "run-bound", candidate_status: "success", predecessor_loop_id: "loop-old", reason: "newest_run_already_bound" },
  });
  assert.match(bound, /run-bound/);
  assert.match(bound, /已属于另一个 Loop/);
  assert.doesNotMatch(bound, /首轮将绑定直接用户 Run/);
});
