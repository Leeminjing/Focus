/* 生产 Patrol 页的顶部状态回归：中英文、窄容器、手机宽度及 200% 缩放。
 * 运行 node desktop/tests/e2e/patrol-workbench-status.e2e.cjs；FOCUS_WORKBENCH_EVIDENCE 指定截图目录。
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
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-workbench-status-")));
  const output = process.env.FOCUS_WORKBENCH_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-workbench-status-evidence-"));
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
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'status',display_name:'运行状态验收',path:'C:/isolated/status'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='验证运行状态';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('patrolFixture.connected("loop-status")');
    let loopRevision = 1;
    for (const locale of ['zh-CN', 'en-US']) {
      await run(`FocusI18n.setLocale(${JSON.stringify(locale)})`);
      await until('!document.querySelector("[data-patrol-content]").disabled');
      await run(`if(document.querySelector('[data-patrol-details-content]').hidden) document.querySelector('[data-patrol-composer] [data-patrol-details]').click()`);
      for (const [width, zoom, available] of [[1748, 1], [1100, 1], [1000, 1], [999, 1], [900, 1], [390, 1], [320, 1], [1280, 2], [1440, 1, 900]]) {
        win.webContents.setZoomFactor(zoom); win.setContentSize(width, 844);
        await run(`document.querySelector('#app').style.width=${JSON.stringify(available ? available + 'px' : '')}`);
        await sleep(100);
        for (const [state, label] of [['running', '推进中'], ['paused', '已暂停'], ['failed', '执行失败']]) {
          await run(`patrolFixture.emit('loop-status','loop','loop-status',${++loopRevision},{status:${JSON.stringify(state)},interaction_mode:'workspace_patrol'})`);
          await until(`document.querySelector('[data-workbench-status]').textContent.includes(FocusI18n.text(${JSON.stringify(label)}))`);
          const status = await run(`(() => {
            const node=document.querySelector('[data-workbench-status]'), r=node.getBoundingClientRect(), heading=node.closest('header').getBoundingClientRect();
            return {visible:getComputedStyle(node).display!=='none' && r.width>0 && r.height>0,
              contained:r.left>=heading.left && r.right<=heading.right+1 && r.top>=heading.top && r.bottom<=heading.bottom+1,
              text:node.innerText,title:node.title,width:document.querySelector('#app').clientWidth,
              overflow:document.documentElement.scrollWidth>innerWidth+1};
          })()`);
          const expected = await run(`FocusI18n.text(${JSON.stringify(label)})`);
          assert.ok(status.visible, `${locale} ${width}px/${zoom}: ${state} must remain visible`);
          assert.ok(status.contained && !status.overflow, JSON.stringify({locale, width, zoom, state, status}));
          assert.ok(status.text.includes(expected));
          assert.ok(status.title.includes(expected) && status.title.includes(await run(`FocusI18n.text('实时更新')`)), 'tooltip retains state and connection details');
          if (status.width < 1000) assert.equal(status.text, expected, 'narrow header shows the short state');
          else assert.equal(status.text, status.title, 'wide header retains full status');
          if (locale === 'zh-CN' && width === 390) await capture(`status-${state}-${width}`);
        }
      }
    }
    console.log(`PASS Patrol workbench status; evidence: ${output}`); win.destroy(); app.quit();
  }).catch(error => {console.error(error); app.exit(1);});
}
