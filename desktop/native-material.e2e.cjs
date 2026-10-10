/* 本文件对外提供系统材质隔离验收入口。输入为 FOCUS_NATIVE_EVIDENCE 输出目录；输出为原生窗口截图和 JSON 结果。
 * 工作流为独立 userData 加载生产六页，在两个受控后景前将测试窗口置顶并聚焦后捕获，记录焦点与页面透明层，检查系统合成变化、原生最大化/还原和主题监听清理。
 * 示例：node desktop/native-material.e2e.cjs。网络由原 fixture 提供；只捕获本测试窗口，不采集用户桌面。
 */
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const { supportsAcrylic, bindWindowMaterial } = require("./window-material.cjs");
const output = process.env.FOCUS_NATIVE_EVIDENCE || path.join(__dirname, "../openspec/changes/refactor-focus-reference-glass-frontend/evidence/native");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 90000, windowsHide: true });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow, nativeTheme, nativeImage, screen } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-native-material-")));
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  app.whenReady().then(async () => {
    fs.mkdirSync(output, { recursive: true });
    assert.ok(supportsAcrylic(process.platform, os.release()));
    nativeTheme.themeSource = "light";
    const area = screen.getPrimaryDisplay().workArea;
    const bounds = { x: area.x + 24, y: area.y + 24, width: Math.min(1440, area.width - 48), height: Math.min(800, area.height - 48) };
    const behind = new BrowserWindow({ ...area, show: false, frame: false, webPreferences: { sandbox: true } });
    await behind.loadURL("data:text/html,<body style='margin:0;background:%233274b5'></body>");
    behind.showInactive();
    const win = new BrowserWindow({ ...bounds, backgroundColor: "#00000000", backgroundMaterial: "acrylic", titleBarStyle: "hidden", titleBarOverlay: { height: 56, color: "#00000000", symbolColor: "#142039" }, show: false, webPreferences: { preload: path.join(__dirname, "workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false } });
    const listeners = nativeTheme.listenerCount("updated");
    bindWindowMaterial(win, nativeTheme, true);
    await win.loadFile(path.join(__dirname, "index.html"));
    await win.webContents.executeJavaScript("document.documentElement.dataset.nativeMaterial='acrylic'");
    win.show(); win.focus(); await sleep(1400);
    const captures = [];
    const capture = async name => {
      win.moveTop(); win.focus(); await sleep(200);
      captures.push({name, focused:win.isFocused(), layers:await win.webContents.executeJavaScript(`['html','body','.app-frame','#app'].map(selector=>({selector,background:getComputedStyle(document.querySelector(selector)).backgroundColor}))`)});
      const bounds = win.getBounds();
      const left = Math.max(area.x, bounds.x), top = Math.max(area.y, bounds.y);
      const r = screen.dipToScreenRect(win, { x: left, y: top, width: Math.min(area.x + area.width, bounds.x + bounds.width) - left, height: Math.min(area.y + area.height, bounds.y + bounds.height) - top });
      const [x,y,w,h] = [r.x,r.y,r.width,r.height];
      const file = path.join(output,name+".png");
      const script = `Add-Type -AssemblyName System.Drawing\nAdd-Type -TypeDefinition 'using System.Runtime.InteropServices; public class CaptureDpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'\n[CaptureDpi]::SetProcessDPIAware() | Out-Null\n$b=New-Object Drawing.Bitmap ${w},${h}\n$g=[Drawing.Graphics]::FromImage($b)\n$g.CopyFromScreen(${x},${y},0,0,$b.Size)\n$b.Save('${file.replace(/'/g,"''")}')\n$g.Dispose()\n$b.Dispose()`;
      require("node:child_process").execFileSync("powershell.exe",["-NoProfile","-NonInteractive","-Command",script],{windowsHide:true});
      return nativeImage.createFromPath(file);
    };
    const blue = await capture("blue-backdrop");
    await behind.webContents.executeJavaScript("document.body.style.background='#ce814b'");
    await sleep(1400);
    const orange = await capture("orange-backdrop");
    const a = blue.toBitmap(), b = orange.toBitmap();
    let difference = 0;
    for (let i = 0; i < Math.min(a.length, b.length); i += 4) difference += Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);
    const meanDifference = difference / (Math.min(a.length,b.length)/4*3);
    const run = code => win.webContents.executeJavaScript(code, true);
    const until = async code => {
      const end = Date.now() + 8000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > end) throw new Error(`Native page timeout: ${code}`);
        await sleep(30);
      }
    };
    await behind.webContents.executeJavaScript("document.body.style.background='linear-gradient(120deg,#c2d2e2,#f2f4f7 48%,#d3dfd2)'");
    await sleep(500); await capture("patrol-unbound");
    const referenceGeometry = await run(`(() => {
      const rect = selector => { const {x,y,width,height} = document.querySelector(selector).getBoundingClientRect(); return {x,y,width,height}; };
      return { viewport: { width:innerWidth, height:innerHeight }, navigation:rect('.app-navigation'), heading:rect('.patrol-quiet-heading'), composer:rect('[data-patrol-composer]') };
    })()`);
    fs.writeFileSync(path.join(output,"reference-geometry.json"), JSON.stringify(referenceGeometry,null,2));
    await run("state.patrolWorkspace={workspace_id:'native',display_name:'材质验收',path:'C:/isolated/native'}; render()");
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await sleep(500); await capture("patrol-quiet");
    await run('document.querySelector("[data-patrol-content]").value="隔离验收输入"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()');
    await until('window.patrolFixture.connected("loop-native")');
    await run('window.patrolFixture.lineage={roots:{},nodes:[{context_id:"root",revision_id:"r1",generation:1}],edges:[],complete:true}; window.patrolFixture.emit("loop-native","context","root",1,{title:"已提交工作线",status:"active"}); document.querySelector("[data-patrol-composer] [data-patrol-details]").click()');
    await until('document.querySelector("[data-patrol-lineage] [data-context-id=root]")');
    await sleep(400); await capture("patrol-details");
    assert.equal(await run('document.querySelector(".patrol-facts-card header").getBoundingClientRect().bottom < document.querySelector("[data-patrol-composer]").getBoundingClientRect().top'), true);
    for (const [action,page] of [["focus-home","task"],["show-map","map"],["show-plugins","plugins"],["show-memory","memory"],["show-assembly","standalone"]]) {
      await run(`document.querySelector('[data-action=${action}]:not([data-patrol-switch])').click()`);
      await sleep(700); await capture(page);
    }
    const normal = win.getBounds();
    win.maximize(); await sleep(500); assert.ok(win.isMaximized());
    await capture("maximized");
    win.unmaximize(); await sleep(500); assert.ok(!win.isMaximized());
    win.minimize(); await sleep(200); assert.ok(win.isMinimized());
    win.restore(); await sleep(400); win.focus();
    win.webContents.setZoomFactor(2); win.setTitleBarOverlay({height:112}); await sleep(300);
    await capture("zoom-200");
    const theme = { prefersReducedTransparency: nativeTheme.prefersReducedTransparency, highContrast: nativeTheme.shouldUseHighContrastColors };
    win.destroy(); behind.destroy();
    assert.equal(nativeTheme.listenerCount("updated"), listeners);
    const result = { windows:os.release(), electron:process.versions.electron, meanDifference, nativeTransparencyObserved:meanDifference>2, captures, theme, normal, restored:true, minimized:true, zoom:2, listenerCleanup:true, fixture:true };
    fs.writeFileSync(path.join(output,"report.json"), JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
    assert.ok(meanDifference>2, "Native transparency has not been visually confirmed");
    app.quit();
  }).catch(error => { fs.mkdirSync(output,{recursive:true}); fs.writeFileSync(path.join(output,"failure.txt"),error.stack); console.error(error); app.exit(1); });
}
