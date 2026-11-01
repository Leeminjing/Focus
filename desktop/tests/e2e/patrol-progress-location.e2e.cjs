/* 验证生产 Patrol 页定位进度只展示图谱，由用户点击节点打开详情。
 * 运行：node desktop/tests/e2e/patrol-progress-location.e2e.cjs。
 * 复用离线 API fixture，覆盖宽屏、窄屏及手机宽度；截图写入 FOCUS_WORKBENCH_EVIDENCE 或临时目录。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 60000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.disableHardwareAcceleration();
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-progress-location-")));
  const output = process.env.FOCUS_WORKBENCH_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-progress-location-evidence-"));
  fs.mkdirSync(output, { recursive: true });
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1748, height: 904, webPreferences: {
      preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    } });
    const run = code => win.webContents.executeJavaScript(code, true);
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async code => {
      const end = Date.now() + 10000;
      while (!await run(`Boolean(${code})`)) { if (Date.now() > end) throw new Error(`Timeout: ${code}`); await sleep(20); }
    };
    const click = async selector => {
      const point = await run(`(() => { const node = document.querySelector(${JSON.stringify(selector)}); node.scrollIntoView({block:'nearest'}); const r = node.getBoundingClientRect(); return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}; })()`);
      win.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...point });
      win.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...point });
      await sleep(80);
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'location',display_name:'定位工作线回归',path:'C:/isolated/location'}; render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='验证工作线定位'; document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input')); document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('patrolFixture.connected("loop-location")');
    const cases = [
      { ids: ['ctx-a'], notice: '1 条关联工作线' },
      { ids: ['ctx-a', 'ctx-b'], notice: '2 条关联工作线' },
      { ids: [], notice: '尚未关联' },
      { ids: ['unpublished'], notice: '尚未发布' },
    ];
    await run(`patrolFixture.graphConversations=true;
      patrolFixture.lineage={complete:true,roots:{'ctx-a':'rev-a','ctx-b':'rev-b'},nodes:[{context_id:'ctx-a',revision_id:'rev-a',generation:1},{context_id:'ctx-b',revision_id:'rev-b',generation:1}],edges:[]};
      document.querySelector('[data-patrol-composer] [data-patrol-details]').click();
      patrolFixture.emit('loop-location','task_progress','progress',1,{progress_id:'p1',generation:1,document:{items:${JSON.stringify(cases.map((item, i) => ({ item_id: `item-${i}`, description: `定位条目 ${i}`, state: 'in_progress', support: 'unknown', context_ids: item.ids })))}}});`);
    await until('document.querySelectorAll(".workbench-node").length===2 && document.querySelectorAll("[data-patrol-progress-locate]").length===4');
    for (const width of [1748, 900, 390]) {
      win.setContentSize(width, 904); await sleep(200);
      for (const { ids, notice } of cases) {
        if (width < 1200) await click('[data-workbench-show="progress"]');
        await click(`[data-patrol-progress-locate='${JSON.stringify(ids)}']`);
        await until(`document.querySelector('[data-workbench-location]').textContent.includes(${JSON.stringify(notice)})`);
        assert.equal(await run('document.querySelector(".workspace-patrol").dataset.workbenchPanel'), 'graph', `${width}px: locating progress must stay on the graph`);
        assert.equal(await run('getComputedStyle(document.querySelector(".patrol-graph-card")).display !== "none"'), true);
        assert.equal(await run('document.querySelectorAll(".workbench-node.is-selected").length'), 0, 'locating must not select a Context');
        assert.deepEqual(await run('Array.from(document.querySelectorAll(".workbench-node.is-related"), node => node.dataset.contextId).sort()'), ids.filter(id => id !== 'unpublished').sort());
        if (ids.length === 1 && ids[0] === 'ctx-a') fs.writeFileSync(path.join(output, `located-${width}.png`), (await win.webContents.capturePage()).toPNG());
      }
      await click('[data-patrol-lineage] [data-context-id="ctx-a"]');
      await until('document.querySelector(".workspace-patrol").dataset.workbenchPanel==="context"');
      assert.equal(await run('document.querySelector(".workbench-node.is-selected").dataset.contextId'), 'ctx-a');
      assert.equal(await run('getComputedStyle(document.querySelector(".patrol-selected-card")).display !== "none"'), true);
      await click('[data-patrol-context-close]');
      await until('document.querySelectorAll(".workbench-node.is-selected").length===0');
    }
    assert.equal(await run('document.querySelector("[data-patrol-error]").textContent'), '');
    console.log("PROGRESS_LOCATION_PASS", output); win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
