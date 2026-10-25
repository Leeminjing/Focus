/* 生产 Loop 单列布局回归：视口高度、图谱收放、草稿与滚动保持、宽屏恢复。
 * 运行 node desktop/tests/e2e/loop-console-layout.e2e.cjs；FOCUS_LOOP_LAYOUT_EVIDENCE 指定截图及报告目录。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
if (!process.versions.electron) {
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], {env, encoding: "utf8", windowsHide: true, timeout: 60000});
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const {app, BrowserWindow} = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-loop-layout-")));
  app.disableHardwareAcceleration();
  const output = process.env.FOCUS_LOOP_LAYOUT_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-loop-layout-evidence-"));
  fs.mkdirSync(output, {recursive: true});
  app.whenReady().then(async () => {
    const win = new BrowserWindow({show: false, width: 1100, height: 800, webPreferences: {
      preload: path.join(__dirname, "../fixtures/agent-loop-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    }});
    const run = async code => {
      try { return await win.webContents.executeJavaScript(code, true); }
      catch (error) { throw new Error(`${code}: ${error.message}`); }
    };
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async code => {
      const end = Date.now() + 10000;
      while (!await run(`Boolean(${code})`)) { if (Date.now() > end) throw new Error(`Timeout: ${code}`); await sleep(20); }
    };
    const capture = async name => {
      await run(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
      await sleep(80);
      fs.writeFileSync(path.join(output, `${name}.png`), (await win.webContents.capturePage()).toPNG());
    };
    const geometry = () => run(`(()=>{
      const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
      return {viewport:[innerWidth,innerHeight],main:rect('.loop-console-main'),workspace:rect('.loop-console-workspace'),map:rect('.loop-console-map'),conversation:rect('.loop-console-conversation'),transcript:rect('[data-loop-transcript]')};
    })()`);
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('typeof openLoopView === "function" && state.activeTaskId');
    await run(`(async()=>{
      state.activeTaskId='root';await hydrateActive('root');await openLoopView();
      const form=document.querySelector('#agentLoopStartForm');
      form.querySelector('[data-mission-outcome]').value='验证 Loop 单列布局';
      form.querySelector('[data-mission-boundary="required_invariants"]').value='保持 Context 来源';
      form.querySelector('[data-check-claim]').value='图谱与会话可参照阅读';
      form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
    })()`);
    await until('document.querySelector("[data-loop-transcript]") && loopLiveStore.get().connection.status === "live"');
    const results = [];
    for (const [width, height, zoom] of [[1100, 650, 1], [1100, 900, 1], [900, 800, 1], [390, 844, 1], [1280, 900, 2]]) {
      win.setContentSize(width, height); win.webContents.setZoomFactor(zoom); await sleep(150);
      const layout = await geometry(); results.push({width, height, zoom, ...layout});
      const detail = JSON.stringify(layout);
      assert.ok(layout.workspace.height >= 280 && layout.workspace.height <= 421, detail);
      assert.ok(layout.conversation.height >= 400 && layout.conversation.height <= 641, detail);
      assert.ok(layout.main.height < 1110, detail);
      assert.ok(layout.transcript.height >= 120, detail);
      assert.ok(layout.conversation.top >= layout.workspace.bottom - 1, detail);
      await run(`document.querySelector('.loop-console-map-controls').scrollIntoView({block:'start'})`);
      await capture(`expanded-${width}-${height}-${zoom}`);
    }
    win.webContents.setZoomFactor(1); win.setContentSize(1100, 800); await sleep(150);
    const expanded = await geometry();
    const viewport = await run(`(()=>{
      window.savedMap=document.querySelector('.portfolio-map-scroll');savedMap.scrollTop=100;savedMap.scrollLeft=100;
      window.savedDraft=document.querySelector('#loopInterventionForm textarea');savedDraft.value='保留会话草稿';
      return {top:savedMap.scrollTop,left:savedMap.scrollLeft};
    })()`);
    await run(`document.querySelector('[data-loop-console-map-toggle]').focus()`);
    for (const type of ["keyDown", "char", "keyUp"]) win.webContents.sendInputEvent({type, keyCode: "Enter"});
    await sleep(100);
    assert.equal(await run(`document.querySelector('[data-loop-console-map-toggle]').getAttribute('aria-expanded')`), "false");
    assert.equal(await run(`getComputedStyle(document.querySelector('.loop-console-workspace')).display`), "none");
    assert.ok(expanded.main.height - (await geometry()).main.height >= expanded.workspace.height - 1);
    assert.equal(await run(`savedDraft===document.querySelector('#loopInterventionForm textarea') && savedDraft.value==='保留会话草稿'`), true);
    await capture("collapsed");
    win.setContentSize(1440, 900); await sleep(150);
    assert.equal(await run(`getComputedStyle(document.querySelector('.loop-console-workspace')).display`), "grid");
    assert.equal(await run(`getComputedStyle(document.querySelector('.loop-console-map-controls')).display`), "none");
    win.setContentSize(1100, 800); await sleep(150);
    assert.equal(await run(`getComputedStyle(document.querySelector('.loop-console-workspace')).display`), "none");
    await run(`renderLoop()`); await sleep(100);
    assert.equal(await run(`document.querySelector('[data-loop-console-map-toggle]').getAttribute('aria-expanded')`), "false");
    assert.equal(await run(`document.querySelector('#loopInterventionForm textarea').value`), "保留会话草稿");
    await run(`document.querySelector('[data-loop-console-map-toggle]').click()`); await sleep(100);
    assert.equal(await run(`document.querySelector('[data-loop-console-map-toggle]').getAttribute('aria-expanded')`), "true");
    assert.deepEqual(await run(`({top:document.querySelector('.portfolio-map-scroll').scrollTop,left:document.querySelector('.portfolio-map-scroll').scrollLeft})`), viewport);
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({results, checks: ["viewport heights", "readable transcript", "keyboard collapse", "draft preservation", "wide layout restoration", "rebind state and listener cleanup", "map scroll restoration"]}, null, 2));
    console.log(`PASS Loop console layout; evidence: ${output}`); win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
