/* 在隐藏 Electron 中验证生产诊断入口、重复点击、失败反馈、Live 刷新与迟到结果隔离。
 * 离线 HTTP fixture 只用于页面接线；数据库重放由 test_fact_projection_repair.py 覆盖。
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
if (!process.versions.electron) {
  require("node:test")("fact projection diagnostics and repair in the production workbench", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-projection-ui-"));
    const env = { ...process.env, FOCUS_PROJECTION_USER_DATA: directory };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], {
      env, encoding: "utf8", timeout: 90000, windowsHide: true,
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /PROJECTION_UI_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_PROJECTION_USER_DATA);
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 900,
      webPreferences: { preload: path.join(__dirname, "workspace-patrol-test-preload.cjs"), contextIsolation: false, nodeIntegration: true } });
    const errors = [];
    win.webContents.on("console-message", (_event, level, message) => { if (level === 3) errors.push(message); });
    const run = expression => win.webContents.executeJavaScript(expression, true);
    const until = async expression => {
      const deadline = Date.now() + 10000;
      while (!await run(`Boolean(${expression})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${expression}`);
        await new Promise(resolve => setTimeout(resolve, 25));
      }
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until('workspacePatrolIntake && state.tasks.length > 0');
    await run(`
      window.projectionFixture = { calls: 0, liveReads: 0, release: null, mode: "success", rows: [{
        failure_id: "failure-1", loop_id: "loop-projection", projector_name: "loop_facts", unit_kind: "journal_event",
        unit_id: "source-event", status: "quarantined", error_class: "ValueError", attempt_count: 1,
        boundary_sequence: 4, last_failed_at: "2026-10-18T08:00:00Z", can_repair: true,
      }] };
      window.fileFeedbackFetch = async (input, options) => {
        const url = new URL(String(input), "http://focus.test"), fixture = projectionFixture;
        if (url.pathname.endsWith("/live")) fixture.liveReads++;
        if (url.pathname.endsWith("/projection-failures")) return Response.json({
          items: fixture.rows.filter(row => url.searchParams.get("include_resolved") === "true" || row.status !== "resolved"), has_more: false,
        });
        if (url.pathname.endsWith("/projection-failures/failure-1/repair")) {
          fixture.calls++;
          await new Promise(resolve => { fixture.release = resolve; });
          if (fixture.mode === "network") return new Response("连接中断", { status: 503 });
          const failure = fixture.rows[0];
          failure.status = fixture.mode === "success" ? "resolved" : "quarantined";
          failure.can_repair = failure.status === "quarantined";
          failure.attempt_count++;
          return Response.json({ processed: fixture.mode === "success" ? 1 : 0, failure });
        }
      };
      patrolFixture.seedHistory("projection", []);
      state.patrolWorkspace = { workspace_id: "projection", display_name: "Projection QA" };
      state.view = "patrol"; render();
    `);
    await until('patrolFixture.connected("loop-projection")');
    await run('document.querySelector("[data-patrol-composer] [data-patrol-details]").click(); document.querySelector("[data-patrol-projection-open]").click()');
    await until('document.querySelector("[data-patrol-projection-repair]")');
    await run('document.querySelector(".patrol-projection-failure").open = true');
    assert.match(await run('document.querySelector("[data-patrol-dialog-body]").textContent'), /source-event/);
    await run('workspacePatrolIntake.store("projection").edit({ content: "保留输入草稿" }); document.querySelector("[data-patrol-projection-repair]").click(); document.querySelector("[data-patrol-projection-repair]").click()');
    await until('projectionFixture.calls === 1');
    assert.equal(await run('document.querySelector("[data-patrol-projection-repair]").disabled'), true);
    // 迟到结果不得重新打开已切换的授权面板。
    await run('document.querySelector("[data-patrol-grant-open]").click()');
    await until('document.querySelector("#agentLoopGrantForm")');
    await run('projectionFixture.release()');
    await until('projectionFixture.liveReads >= 2');
    assert.equal(await run('document.querySelector("[data-patrol-dialog]").dataset.patrolDialogKind'), "grant");
    await run('document.querySelector("[data-patrol-projection-open]").click()');
    await until('document.querySelector("[data-patrol-projection-resolved]")');
    assert.equal(await run('document.querySelector(".patrol-projection-failure")'), null);
    await run('document.querySelector("[data-patrol-projection-resolved]").click()');
    await until('document.querySelector(".patrol-projection-failure")');
    assert.match(await run('document.querySelector(".patrol-projection-failure").textContent'), /已修复/);
    assert.equal(await run('document.querySelector("[data-patrol-projection-repair]")'), null);

    for (const mode of ["network", "failure", "success"]) {
      await run(`projectionFixture.mode = ${JSON.stringify(mode)}; projectionFixture.rows[0].status = "quarantined"; projectionFixture.rows[0].can_repair = true; document.querySelector("[data-patrol-projection-refresh]").click()`);
      await until('document.querySelector("[data-patrol-projection-repair]")');
      const calls = await run('projectionFixture.calls');
      await run('document.querySelector("[data-patrol-projection-repair]").click()');
      await until(`projectionFixture.calls === ${calls + 1}`);
      await run('projectionFixture.release()');
      await until(`document.querySelector("[data-patrol-dialog-body]").textContent.includes(${JSON.stringify(mode === "network" ? "修复请求未确认" : mode === "failure" ? "重新生成仍失败" : "事实投影已修复")})`);
      if (mode !== "success") assert.equal(await run('document.querySelector("[data-patrol-projection-repair]").textContent'), "修复后重新生成");
    }
    assert.equal(await run('workspacePatrolIntake.store("projection").get().draft.content'), "保留输入草稿");
    assert.deepEqual(errors, []);
    console.log("PROJECTION_UI_PASS");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
