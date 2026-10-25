/* 验证生产 Loop 视图在固定窗口、不同容器宽度及拖拽比例下的布局。
 * 示例：node desktop/tests/e2e/loop-console-layout.e2e.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 30000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-loop-layout-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 960, webPreferences: { offscreen: true, backgroundThrottling: false } });
    const source = file => fs.readFileSync(path.join(__dirname, "../../src/renderer", file), "utf8");
    require("../../src/renderer/features/loop/loop-console-view.js");
    const view = require("../../src/renderer/features/loop/loop-view.js");
    const html = view.render({ snapshot: { loop_id: "layout", status: "paused", usage: {}, grant: { budgets: {} } } }, {}, { manifest: { nodes: [], edges: [] } });
    const styles = ["shared/styles/tokens.css", "shared/styles/base.css", "features/loop/loop-console.css"].map(source).join("\n");
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>${styles}body{margin:0;overflow:auto}#host{width:900px;margin-left:240px}</style><main id="host">${html}</main><script>${source("features/loop/loop-console-store.js")}</script><script>${source("features/loop/loop-console-controller.js")}</script>`));
    const run = code => win.webContents.executeJavaScript(code);
    await run(`window.controller = FocusLoopConsoleController.create({api:{},store:FocusLoopConsoleStore.create()});controller.bind(document.querySelector('#host'));`);
    async function measure(width) {
      return run(`(async () => {
        document.querySelector('#host').style.width = '${width}px';
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const main = document.querySelector('.loop-console-main');
        const shell = document.querySelector('.console-shell');
        const left = document.querySelector('.loop-console-workspace').getBoundingClientRect();
        const right = document.querySelector('.loop-console-conversation').getBoundingClientRect();
        return { viewport: innerWidth, divider: getComputedStyle(document.querySelector('.loop-console-divider')).display,
          width: main.clientWidth, scrollWidth: main.scrollWidth, leftWidth: left.width, rightWidth: right.width,
          stacked: right.top >= left.bottom, shellHeight: shell.clientHeight, mainHeight: main.clientHeight };
      })()`);
    }
    for (const width of [900, 954, 955, 956, 1200]) {
      const result = await measure(width);
      assert.ok(result.viewport > 1100);
      assert.equal(result.scrollWidth, result.width, JSON.stringify(result));
      assert.equal(result.stacked, width < 955, JSON.stringify(result));
      assert.equal(result.divider === "none", width < 955, JSON.stringify(result));
      if (width < 955) assert.ok(result.shellHeight >= result.mainHeight, "单栏内容必须可滚动访问");
      else assert.ok(result.leftWidth >= 520 && result.rightWidth >= 430, JSON.stringify(result));
    }
    // 键盘与指针共用比例更新；在窄容器重绑及调整比例也不能覆盖单栏查询。
    await measure(900);
    await run(`controller.bind(document.querySelector('#host'));for(let i=0;i<8;i++)document.querySelector('.loop-console-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));`);
    assert.equal((await measure(900)).stacked, true);
    for (const width of [955, 1000, 1400]) {
      const result = await measure(width);
      assert.equal(result.scrollWidth, result.width);
      assert.ok(result.leftWidth >= 520 && result.rightWidth >= 430);
    }
    if (process.env.FOCUS_LOOP_LAYOUT_EVIDENCE) {
      fs.mkdirSync(process.env.FOCUS_LOOP_LAYOUT_EVIDENCE, { recursive: true });
      for (const width of [900, 1200]) {
        await measure(width);
        fs.writeFileSync(path.join(process.env.FOCUS_LOOP_LAYOUT_EVIDENCE, `loop-${width}.png`), (await win.webContents.capturePage()).toPNG());
      }
    }
    await run("controller.destroy()");
    console.log("Loop container layout: PASS (900, 954, 955, 956, 1000, 1200, 1400px; resize and ratio changes)");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
