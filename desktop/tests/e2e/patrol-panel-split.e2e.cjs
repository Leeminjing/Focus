/* 生产 Patrol 页的上下分栏回归：短窗口、真实拖拽、键盘边界、比例恢复和窄屏标签。
 * 运行 node desktop/tests/e2e/patrol-panel-split.e2e.cjs；FOCUS_WORKBENCH_EVIDENCE 指定截图及报告目录。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
if (!process.versions.electron) {
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], {env, encoding: "utf8", windowsHide: true, timeout: 60000});
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const {app, BrowserWindow} = require("electron");
  app.disableHardwareAcceleration();
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-split-")));
  const output = process.env.FOCUS_WORKBENCH_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-split-evidence-"));
  fs.mkdirSync(output, {recursive: true});
  app.whenReady().then(async () => {
    const win = new BrowserWindow({show: false, width: 1440, height: 900, webPreferences: {
      preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
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
    const capture = async name => fs.writeFileSync(path.join(output, `${name}.png`), (await win.webContents.capturePage()).toPNG());
    const geometry = () => run(`(()=>{
      const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
      const panels=document.querySelector('[data-patrol-panel-split]');
      return {progress:rect('.patrol-progress-card'),items:rect('[data-patrol-progress] ul'),observation:rect('.patrol-observation-card'),body:rect('.patrol-observation-scroll'),
        column:rect('[data-patrol-panel-split]'),scrollHeight:panels.scrollHeight,clientHeight:panels.clientHeight};
    })()`);
    const key = async name => {
      win.webContents.sendInputEvent({type: "keyDown", keyCode: name});
      win.webContents.sendInputEvent({type: "keyUp", keyCode: name}); await sleep(50);
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'split',display_name:'面板布局验收',path:'C:/isolated/split'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='验证进度与观察';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('patrolFixture.connected("loop-split")');
    await run(`document.querySelector('[data-patrol-composer] [data-patrol-details]').click();
      patrolFixture.emit('loop-split','task_progress','progress',1,{progress_id:'p1',generation:1,document:{items:Array.from({length:16},(_,i)=>({description:'工作项 '+i,item_id:'item-'+i,state:'in_progress',support:'unknown',context_ids:[]}))}});
      patrolFixture.emit('loop-split','round','r1',1,{number:1,observation_id:'observation-1'});`);
    await until('document.querySelector("[data-patrol-progress] li") && document.querySelector(".observation-rounds")');
    const results = [];
    for (const height of [650, 480, 1000]) {
      win.setContentSize(1440, height); await sleep(180);
      const layout = await geometry(); results.push({height, ...layout});
      assert.ok(layout.progress.height >= 360, JSON.stringify(layout));
      assert.ok(layout.observation.height >= 260, JSON.stringify(layout));
      assert.ok(layout.body.height >= 180, JSON.stringify(layout));
      assert.ok(layout.items.height >= 60, JSON.stringify(layout));
      assert.ok(layout.observation.top >= layout.progress.bottom + 10, JSON.stringify(layout));
      if (height <= 650) assert.ok(layout.scrollHeight > layout.clientHeight, JSON.stringify(layout));
      await capture(`height-${height}`);
      if (height <= 650) {
        await run(`(()=>{const panels=document.querySelector('[data-patrol-panel-split]');panels.scrollTop=panels.scrollHeight})()`);
        const scrolled = await geometry();
        assert.ok(scrolled.body.bottom <= scrolled.column.bottom + 1, JSON.stringify(scrolled));
        assert.ok(scrolled.body.top >= scrolled.column.top, JSON.stringify(scrolled));
        await capture(`observation-${height}`);
        await run(`document.querySelector('[data-patrol-panel-split]').scrollTop=0`);
      }
    }
    await run(`window.splitInput=document.querySelector('[data-patrol-content]');splitInput.value='调整后继续输入';splitInput.dispatchEvent(new Event('input'));
      document.querySelector('[data-patrol-panel-resizer]').scrollIntoView({block:'nearest'});`);
    const before = await geometry();
    const point = await run(`(()=>{const r=document.querySelector('[data-patrol-panel-resizer]').getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
    win.webContents.sendInputEvent({type: "mouseDown", ...point, button: "left", clickCount: 1}); await sleep(30);
    win.webContents.sendInputEvent({type: "mouseMove", x: point.x, y: point.y - 80, button: "left"}); await sleep(50);
    win.webContents.sendInputEvent({type: "mouseUp", x: point.x, y: point.y - 80, button: "left", clickCount: 1}); await sleep(80);
    const dragged = await geometry();
    assert.ok(Math.abs(before.progress.height - dragged.progress.height - 80) <= 2, JSON.stringify({before, dragged}));
    assert.equal(await run(`document.activeElement.hasAttribute('data-patrol-panel-resizer')`), true);
    await key("Home"); assert.ok(Math.abs((await geometry()).progress.height - 360) <= 1);
    await key("End"); assert.ok(Math.abs((await geometry()).observation.height - 260) <= 1);
    const end = await geometry(); await key("Up");
    const adjusted = await geometry();
    assert.ok(Math.abs(end.progress.height - adjusted.progress.height - 24) <= 1);
    assert.equal(await run(`splitInput===document.querySelector('[data-patrol-content]') && splitInput.value==='调整后继续输入'`), true);
    await run(`document.querySelector('.observation-rounds').open=true`);
    const expanded = await geometry();
    assert.equal(expanded.observation.height, adjusted.observation.height);
    assert.ok(await run(`(()=>{const body=document.querySelector('.patrol-observation-scroll');return body.scrollHeight>body.clientHeight})()`));
    await capture("adjusted");
    const stored = await run(`localStorage.getItem('focus-patrol-panel-split-v1')`);
    assert.ok(Number(stored) > 0 && Number(stored) < 1);
    await run(`state.view='map';render();state.view='patrol';render()`);
    await until('document.querySelector("[data-patrol-panel-split]")');
    assert.equal(await run(`document.querySelector('[data-patrol-panel-split]').style.getPropertyValue('--patrol-progress-weight')`), `${stored}fr`);
    await run(`document.querySelector('[data-patrol-composer] [data-patrol-details]').click()`); await sleep(200);
    for (const [width, zoom] of [[1100, 1], [900, 1], [390, 1], [1280, 2]]) {
      win.setContentSize(width, 844); win.webContents.setZoomFactor(zoom); await sleep(150);
      assert.equal(await run(`getComputedStyle(document.querySelector('[data-patrol-panel-resizer]')).display`), "none");
      for (const panel of ["progress", "observation"]) {
        await run(`document.querySelector('[data-workbench-show=${panel}]').click()`); await sleep(50);
        const height = await run(`document.querySelector('.patrol-${panel}-card').getBoundingClientRect().height`);
        assert.ok(height > 150, JSON.stringify({width, zoom, panel, height}));
      }
      await capture(`compact-${width}-${zoom}`);
    }
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({results, dragged, stored, checks: ["minimum readable heights", "short column scroll", "pointer drag", "keyboard limits", "draft identity", "remount persistence", "compact tabs"]}, null, 2));
    console.log(`PASS Patrol panel split; evidence: ${output}`); win.destroy(); app.quit();
  }).catch(error => {console.error(error); app.exit(1);});
}
