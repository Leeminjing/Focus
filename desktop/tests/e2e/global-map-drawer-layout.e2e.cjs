/* 验证真实全图页面打开 Context 详情后的可用宽度、上下分栏和关闭恢复。
 * 使用隔离 API fixture；运行 node desktop/tests/e2e/global-map-drawer-layout.e2e.cjs。
 * FOCUS_MAP_EVIDENCE 可指定布局报告及截图目录。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (!process.versions.electron) {
  const env = {...process.env};
  delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], {
    env, encoding: "utf8", windowsHide: true, timeout: 60000,
  });
  process.stdout.write(result.stdout || "");
  process.stderr.write(result.stderr || "");
  process.exit(result.status ?? 1);
} else {
  const {app, BrowserWindow} = require("electron");
  app.disableHardwareAcceleration();
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-map-drawer-")));
  const output = process.env.FOCUS_MAP_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-map-drawer-evidence-"));
  fs.mkdirSync(output, {recursive: true});
  app.whenReady().then(async () => {
    const win = new BrowserWindow({
      show: false, width: 1440, height: 900,
      webPreferences: {
        preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"),
        contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
      },
    });
    const run = code => win.webContents.executeJavaScript(code, true);
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async code => {
      const end = Date.now() + 10000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > end) throw new Error(`Timeout: ${code}`);
        await sleep(20);
      }
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'map',display_name:'布局验收',path:'C:/isolated/map'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='全图详情布局';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('window.patrolFixture.connected("loop-map")');
    await run(`patrolFixture.lineage={complete:true,roots:{root:'r1'},nodes:[{context_id:'root',revision_id:'r1',generation:1}],edges:[]};patrolFixture.mapConsole={loop_id:'loop-map',nodes:[{context_id:'root',title:'布局验收',status:'active'}]};document.querySelector('[data-action=show-map]').click()`);
    await until('document.querySelector(".workbench-node") && !mapPortfolio.loading');
    await run(`document.querySelector('[data-context-id=root]').click()`);
    await until('!document.querySelector("[data-patrol-context]").hidden && document.querySelector("[data-patrol-context-close]")');
    const results = [];
    for (const [width, navWidth, zoom, stacked] of [
      [900, null, 1, true], [1100, null, 1, true], [1200, null, 1, true],
      [1440, null, 1, false], [1300, 120, 1, false], [1300, 288, 1, true],
      [1300, 244, 1, true], [1300, 243, 1, false], [1300, 0, 1, false],
      [1280, null, 2, true],
    ]) {
      win.setContentSize(width, 900);
      win.webContents.setZoomFactor(zoom);
      await run(`document.querySelector('.app-navigation').style.flexBasis=${JSON.stringify(navWidth === null ? "" : `${navWidth}px`)}`);
      await sleep(120);
      const layout = await run(`(()=>{
        const graph=document.querySelector('.map-portfolio-surface').getBoundingClientRect();
        const drawer=document.querySelector('[data-patrol-context]').getBoundingClientRect();
        const workspace=document.querySelector('.app-workspace').getBoundingClientRect();
        const host=document.querySelector('#app');
        return {graphWidth:graph.width,workspaceWidth:workspace.width,stacked:drawer.top>=graph.bottom-1,
          beside:drawer.left>=graph.right-1,overflow:document.documentElement.scrollWidth>innerWidth+2,
          reachable:drawer.bottom<=host.getBoundingClientRect().top+host.scrollHeight+1};
      })()`);
      results.push({width, navWidth, zoom, ...layout});
      fs.writeFileSync(path.join(output, `drawer-${width}-${navWidth ?? 'auto'}-${zoom}.png`), (await win.webContents.capturePage()).toPNG());
      const detail = JSON.stringify(results.at(-1));
      assert.equal(layout.stacked, stacked, detail);
      assert.equal(layout.beside, !stacked, detail);
      assert.ok(layout.graphWidth >= Math.min(600, layout.workspaceWidth - 44), detail);
      assert.equal(layout.overflow, false, detail);
      assert.equal(layout.reachable, true, detail);
    }
    await run(`document.querySelector('[data-patrol-context-close]').click()`);
    await until('document.querySelector("[data-patrol-context]").hidden');
    for (const width of [900, 1440]) {
      win.webContents.setZoomFactor(1);
      win.setContentSize(width, 900);
      await sleep(120);
      assert.equal(await run(`getComputedStyle(document.querySelector('.map-view')).gridTemplateColumns.split(' ').length`), 1);
    }
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(results, null, 2));
    console.log(`PASS global map drawer layout (${results.length} layouts, close recovery); evidence: ${output}`);
    win.destroy();
    app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
