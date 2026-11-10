/** 无头 Chromium 验证待处理/历史分区、折叠刷新、撤回编辑与完整请求重试。
 * 运行：node desktop/tests/e2e/conversation-inputs.browser.cjs。
 * 可选 FOCUS_PLAYWRIGHT_MODULE、FOCUS_BROWSER_EXECUTABLE、FOCUS_INPUTS_QA_DIR。
 */
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require(process.env.FOCUS_PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.FOCUS_BROWSER_EXECUTABLE ? { executablePath: process.env.FOCUS_BROWSER_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript({ content: fs.readFileSync(path.join(__dirname, "../fixtures/context-ui-test-preload.cjs"), "utf8") });
    await page.goto(pathToFileURL(path.join(__dirname, "../../src/renderer/index.html")).href);
    await page.waitForLoadState("networkidle");
    await page.locator('[data-action="focus-home"]').click();
    await page.locator("#mainInput").waitFor();
    await page.locator('[data-action="close-inspector"]').click();
    await page.locator("#appInspector").waitFor({ state: "hidden" });
    await page.evaluate(() => {
      const baseFetch = window.fetch;
      window.inputsHarness = { posts: [], withdrawals: [], fail: true, rows: [] };
      const h = window.inputsHarness;
      h.rows = ["delivered", "started", "accepted", "queued", "blocked", "not_delivered"].map(status => ({
        input_id: status, status, mode: ["queued", "blocked", "started"].includes(status) ? "queue" : "steer",
        run: { message: `输入 ${status}`, skills: [], material_inputs: [] },
        error: status === "blocked" ? "上一轮未成功结束" : null,
      }));
      window.fetch = async (url, options = {}) => {
        const pathname = new URL(String(url), "http://focus.test").pathname;
        const match = pathname.match(/\/main\/inputs(?:\/([^/]+))?$/);
        if (!match) return baseFetch(url, options);
        const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
        if (options.method === "DELETE") {
          const row = h.rows.find(row => row.input_id === match[1]);
          if (row.mode !== "queue" || !["queued", "blocked"].includes(row.status)) return json({ detail: "已开始，不能撤回" }, 409);
          h.withdrawals.push(row.input_id); row.status = "withdrawn"; return json(row);
        }
        if (options.method === "POST") {
          const key = new Headers(options.headers).get("Idempotency-Key"), body = JSON.parse(options.body);
          h.posts.push({ key, body });
          if (h.fail) { h.fail = false; return json({ detail: "回执丢失" }, 500); }
          const row = { ...body, input_id: key, status: "queued" };
          h.rows.push(row); return json(row);
        }
        return json(h.rows.filter(row => row.status !== "withdrawn"));
      };
      const detail = state.details.get(state.activeTaskId);
      detail.conversation_inputs = structuredClone(h.rows);
      detail.active_run = { run_id: "current", status: "running", kind: "main" };
      renderFocus();
    });
    const pending = page.locator(".conversation-input-pending"), history = page.locator(".conversation-input-history");
    assert.equal(await pending.locator("article").count(), 4);
    assert.equal(await history.locator("article").count(), 2);
    assert.equal(await history.evaluate(node => node.open), false);
    assert.equal(await history.locator("button").count(), 0);
    assert.equal(await page.getByRole("button", { name: "撤回", exact: true }).count(), 2);
    assert.equal(await pending.locator('[data-input-status="not_delivered"]').isVisible(), true);
    await history.locator("summary").focus();
    await history.locator("summary").press("Enter");
    assert.equal(await history.evaluate(node => node.open), true);
    // 真实轮询更新和完整 chrome 刷新均保留用户展开的历史。
    await page.evaluate(() => { inputsHarness.rows.find(row => row.status === "accepted").status = "delivered"; });
    await page.waitForFunction(() => document.querySelectorAll(".conversation-input-history article").length === 3);
    await page.evaluate(() => renderFocus());
    assert.equal(await history.evaluate(node => node.open), true);
    await history.locator("summary").click();
    await page.locator('[data-input-status="blocked"] [data-action="withdraw-queued-main"]').click();
    await page.waitForFunction(() => inputsHarness.withdrawals.includes("blocked"));
    await page.locator('[data-input-status="queued"] [data-action="edit-queued-main"]').click();
    await page.waitForFunction(() => document.querySelector("#mainInput").value === "输入 queued");
    assert.deepEqual(await page.evaluate(() => inputsHarness.withdrawals), ["blocked", "queued"]);
    assert.equal(await page.locator('[data-input-status="started"] button').count(), 0);
    // 撤回编辑后，失败重试仍携带原身份、材料备注、必看、技能与权限。
    await page.evaluate(() => {
      const taskId = state.activeTaskId, detail = state.details.get(taskId);
      detail.ui_state.skills = ["original-skill"];
      detail.ui_state = accessMode.writeMode(detail.ui_state, "read-only");
      state.materials.set(taskId, [{ material_id: "image", is_image: true, size_bytes: 50, relative_path: "shot.png" }]);
      state.materialSelections.set(taskId, { bindings: [{ materialId: "image", note: "原备注" }], requiredImageIds: ["image"] });
    });
    await page.locator('[data-action="send-main"]').click();
    await page.locator('[data-action="retry-main"]').waitFor();
    const original = await page.evaluate(() => inputsHarness.posts[0]);
    assert.equal(original.body.mode, "queue");
    assert.deepEqual(original.body.run.skills, ["original-skill"]);
    assert.deepEqual(original.body.run.material_inputs, [{ material_id: "image", note: "原备注" }]);
    assert.deepEqual(original.body.run.must_view_material_ids, ["image"]);
    assert.equal(original.body.run.access_mode, "read-only");
    await page.evaluate(() => { state.details.get(state.activeTaskId).ui_state.skills = ["changed-skill"]; });
    await page.locator('[data-action="retry-main"]').click();
    await page.waitForFunction(() => inputsHarness.posts.length === 2 && !taskRunOperations.recovery(state.activeTaskId));
    assert.deepEqual(await page.evaluate(() => inputsHarness.posts[1]), original);
    if (process.env.FOCUS_INPUTS_QA_DIR) {
      fs.mkdirSync(process.env.FOCUS_INPUTS_QA_DIR, { recursive: true });
      await page.locator(".composer-shell").screenshot({ path: path.join(process.env.FOCUS_INPUTS_QA_DIR, "inputs-collapsed.png") });
      await history.locator("summary").click();
      await page.locator(".composer-shell").screenshot({ path: path.join(process.env.FOCUS_INPUTS_QA_DIR, "inputs-history.png") });
    }
    assert.deepEqual(errors, []);
    console.log("CONVERSATION_INPUTS_BROWSER_PASS");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
