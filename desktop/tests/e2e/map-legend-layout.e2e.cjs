/* 生产全图页面：Context 抽屉开关、窄图谱图例、独立缩放区和控件功能回归。
 * 示例：node desktop/tests/e2e/map-legend-layout.e2e.cjs。
 */
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 30000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-map-legend-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 900, webPreferences: { preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true } });
    const run = code => win.webContents.executeJavaScript(code);
    const until = async code => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > deadline) throw new Error("Timeout: " + code);
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'map',display_name:'测试工作区',path:'C:/isolated/map'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='全图布局验收';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('patrolFixture.connected("loop-map")');
    await run(`patrolFixture.graphConversations=true;patrolFixture.lineage={complete:true,roots:{root:'r1'},nodes:[{context_id:'root',revision_id:'r1',generation:1}],edges:[]};patrolFixture.mapConsole={loop_id:'loop-map',nodes:[{context_id:'root',title:'测试工作线',status:'active'}]};document.querySelector('[data-action=show-map]').click()`);
    await until('document.querySelector("[data-map-graph] .workbench-node")');
    for (const drawer of [false, true]) {
      if (drawer) {
        await run(`document.querySelector('[data-map-graph] [data-context-id=root]').click()`);
        await until('!document.querySelector("[data-patrol-context]").hidden');
      }
      for (const [width,height] of [[1440,900],[1100,800],[900,800],[390,844]]) {
        win.setContentSize(width,height);
        await new Promise(resolve=>setTimeout(resolve,120));
        const result = await run(`(async()=>{
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          const surface=document.querySelector('.map-portfolio-surface'),legend=surface.querySelector('.workbench-legend');
          const r=surface.getBoundingClientRect(),l=legend.getBoundingClientRect(),z=surface.querySelector('.portfolio-map-zoom').getBoundingClientRect(),g=surface.querySelector('.portfolio-map-scroll').getBoundingClientRect();
          return {window:innerWidth,graphWidth:r.width,legendInset:parseFloat(getComputedStyle(legend).paddingLeft),
            noOverlap:z.bottom<=l.top+1,canvasAboveControls:g.bottom<=z.top+1,canvasHeight:g.height,
            contained:legend.scrollWidth<=legend.clientWidth&&z.left>=r.left&&z.right<=r.right+1&&l.bottom<=r.bottom+1,
            items:[...legend.children].every(node=>{const b=node.getBoundingClientRect();return b.left>=l.left&&b.right<=l.right+1})};
        })()`);
        const label=JSON.stringify({drawer,...result});
        assert.ok(result.legendInset<=16,label);
        assert.ok(result.noOverlap&&result.canvasAboveControls&&result.canvasHeight>0&&result.contained&&result.items,label);
        if (process.env.FOCUS_MAP_LEGEND_EVIDENCE) {
          fs.mkdirSync(process.env.FOCUS_MAP_LEGEND_EVIDENCE,{recursive:true});
          fs.writeFileSync(path.join(process.env.FOCUS_MAP_LEGEND_EVIDENCE,`legend-${width}-${drawer}.png`),(await win.webContents.capturePage()).toPNG());
        }
        console.log(label);
      }
      win.setContentSize(1440,900);
    }
    const zoom=await run(`(()=>{const before=Number(document.querySelector('.portfolio-map').dataset.zoom);document.querySelector('[data-portfolio-zoom=in]').click();return {before,after:Number(document.querySelector('.portfolio-map').dataset.zoom)}})()`);
    assert.ok(zoom.after>zoom.before,JSON.stringify(zoom));
    console.log("Map legend layout: PASS (8 layouts, Context drawer, zoom controls)");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
