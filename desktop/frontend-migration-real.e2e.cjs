/* 本文件对外提供真实 PostgreSQL/HTTP/SSE 的 Patrol 浏览器迁移验证。
 * 输入为隔离服务、工作区、结果文件和截图目录；输出为首输入/连续表达、真实受理、冻结身份、暂停保持、工作台精确版本选择后再次提交、收回再提交及检查不改目标证据。
 * 具体工作流为隐藏 Electron 加载生产页面，关键业务全部走 HTTP；等待测试 Bootstrap 发布真实 Revision 后检查。
 * 示例：electron frontend-migration-real.e2e.cjs；非 Patrol 页的静态壳 fixture 不作为业务证据。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-migration-real-")));
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1600, height: 1080, webPreferences: { preload: path.join(__dirname, "frontend-migration-real-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true } });
  win.webContents.on("console-message", (_event, level, message) => { if (level >= 3) console.error(message); });
  const evaluate = async code => {
    try { return await win.webContents.executeJavaScript(code, true); }
    catch (error) { throw new Error(`${error.message}: ${code}`, { cause: error }); }
  };
  const wait = async code => {
    const end = Date.now() + 20000;
    while (!await evaluate(`Boolean(${code})`)) { if (Date.now() > end) throw new Error("UI timeout: " + code); await new Promise(resolve => setTimeout(resolve, 30)); }
  };
  const snapshot = async name => {
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    win.webContents.invalidate();
    await new Promise(resolve => setTimeout(resolve, 100));
    fs.writeFileSync(path.join(process.env.FOCUS_FRONTEND_EVIDENCE, name + ".png"), (await win.webContents.capturePage()).toPNG());
  };
  await win.loadFile(path.join(__dirname, "index.html"));
  await wait('document.body.dataset.view === "patrol" && document.querySelector("[data-patrol-content]")');
  await evaluate(`state.patrolWorkspace = ${process.env.FOCUS_FRONTEND_WORKSPACE}; render()`);
  await wait('!document.querySelector("[data-patrol-content]").disabled');
  await snapshot("real-patrol-quiet");
  for (const type of ["information", "outcome", "boundary", "completion_check"]) {
    await evaluate(`document.querySelector("[data-patrol-type]").value=${JSON.stringify(type)}; document.querySelector("[data-patrol-type]").dispatchEvent(new Event("change")); document.querySelector("[data-patrol-content]").value=${JSON.stringify("真实迁移表达 " + type)}; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()`);
    await wait('document.querySelector("[data-patrol-receipt]").textContent.includes("已受理")');
    assert.equal(await evaluate('document.querySelector("[data-patrol-details-content]").hidden'), true);
  }
  const binding = await evaluate("loopApi.workspacePatrol(state.patrolWorkspace.workspace_id)");
  fs.writeFileSync(process.env.FOCUS_FRONTEND_RESULT, JSON.stringify({ ready: true, loop_id: binding.loop_id }));
  await evaluate('document.querySelector("[data-patrol-composer] [data-patrol-details]").click()');
  await wait('!document.querySelector("[data-patrol-observation-open]").disabled');
  await wait('document.querySelector("[data-patrol-lineage] [data-context-id]")');
  await snapshot("real-patrol-details");
  await evaluate('document.querySelector("[data-patrol-observation-open]").click(); document.querySelector("[data-patrol-observation-section=sources]").click()');
  await wait('document.querySelector(".observation-inspection").textContent.includes("已读取")');
  const frozen = await evaluate('document.querySelector(".observation-inspection").textContent');
  await snapshot("real-observation");
  await evaluate('document.querySelector("[data-patrol-observation-progress]").click()');
  await wait('document.querySelector("[data-patrol-progress-version]")');
  await snapshot("real-progress");
  await evaluate('document.querySelector("[data-patrol-dialog-close]").click(); document.querySelector("[data-patrol-facts-open]").click()');
  await wait('document.querySelector("[data-patrol-dialog-body]").textContent.includes("真实持久事实，数量未知")');
  await snapshot("real-facts");
  await evaluate('document.querySelector("[data-patrol-dialog-body] button[data-fact-id=frontend-unknown-fact]").click()');
  await wait('document.querySelector(".fact-inspection")?.textContent.includes("不计为成功")');
  await snapshot("real-fact-detail");
  await evaluate('document.querySelector("[data-patrol-dialog-close]").click(); document.querySelector("[data-patrol-control=pause]").click()');
  await wait('document.querySelector("[data-patrol-state]").textContent.includes("已暂停")');
  await evaluate('document.querySelector("[data-patrol-lineage] [data-context-id]").click()');
  await wait('document.querySelector("[data-patrol-context-tab=conversation]")');
  await evaluate('document.querySelector("[data-patrol-context-tab=conversation]").click()');
  await wait('document.querySelector("[data-patrol-context-content]").textContent.includes("真实迁移表达")');
  await snapshot("real-context");
  const inspected = await evaluate('document.querySelector("[data-patrol-context-content]").textContent');
  await evaluate('document.querySelector("[data-patrol-content]").value="查看已提交版本后继续工作区表达"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()');
  await wait('document.querySelector("[data-patrol-history]").textContent.includes("查看已提交版本后继续工作区表达")');
  await wait('document.querySelector("[data-patrol-receipt]").textContent.includes("已受理")');
  assert.equal(await evaluate('document.querySelector("[data-patrol-context-content]").textContent'), inspected);
  assert.equal(await evaluate('document.body.dataset.view'), "patrol");
  await evaluate('window.closedLoopForm=document.querySelector("[data-patrol-composer]"); document.querySelector("[data-patrol-context-close]").click(); closedLoopForm.querySelector("[data-patrol-details]").click()');
  await wait('document.querySelector("[data-patrol-details-content]").hidden && !document.querySelector("[data-patrol-context-close]")');
  await evaluate('document.querySelector("[data-patrol-content]").value="暂停后继续表达"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()');
  await wait('document.querySelector("[data-patrol-history]").textContent.includes("暂停后继续表达")');
  await wait('document.querySelector("[data-patrol-receipt]").textContent.includes("已受理")');
  assert.equal(await evaluate('document.body.dataset.view'), "patrol");
  assert.equal(await evaluate('closedLoopForm===document.querySelector("[data-patrol-composer]") && document.querySelector("[data-patrol-details-content]").hidden'), true);
  const state = await evaluate('document.querySelector("[data-patrol-state]").textContent');
  assert.ok(state.includes("已暂停"), state);
  fs.writeFileSync(process.env.FOCUS_FRONTEND_RESULT, JSON.stringify({ complete: true, loop_id: binding.loop_id, frozen, state }));
  win.destroy(); app.quit();
}).catch(error => {
  fs.writeFileSync(process.env.FOCUS_FRONTEND_RESULT, JSON.stringify({ error: error.stack }));
  console.error(error); app.exit(1);
});
