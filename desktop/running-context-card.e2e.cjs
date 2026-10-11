/* 本文件对外提供共享运行卡的真实 Chromium 局部更新与视觉合同验收。
 * 输入为隔离图和具名角色预览 fixture；输出为两行尾部、纯文本、固定尺寸、精确 Run 匹配、DOM/焦点/视口稳定及截图证据。
 * 工作流为加载生产图脚本与样式，更新同一按钮的正文，检查布局边界并验证状态/缩放/历史切换；不模拟或声称生产 SSE 已贯通。
 * 示例：node desktop/running-context-card.e2e.cjs；FOCUS_PREVIEW_EVIDENCE 指定可选截图目录。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 60000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-card-preview-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 900, webPreferences: { offscreen: true, backgroundThrottling: false } });
    win.webContents.on("console-message", (event) => { if (event.level === "error") console.error(event.message); });
    const source = file => fs.readFileSync(path.join(__dirname, file), "utf8");
    const styles = ["styles/tokens.css", "styles/base.css", "styles/loop-console.css"].map(source).join("\n");
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><html><head><meta charset="utf-8"><style>${styles} body{margin:0;background:#d7d9d9} #graph{width:100vw;height:100vh}</style></head><body><main id="graph"></main><script>${source("portfolio-map-layout.js")}</script><script>${source("portfolio-map-view.js")}</script></body></html>`));
    const run = script => win.webContents.executeJavaScript(`(()=>{try{return eval(${JSON.stringify(script)})}catch(error){throw new Error(error.stack)}})()`, true).catch(error => { throw new Error(script + "\n" + error.message); });
    await run(`
      window.host = document.querySelector('#graph');
      window.manifest = {nodes: Array.from({length:8}, (_,i)=>({context_id:'context-'+i,title:i===0?'你好':'并行运行 '+i,current_revision_id:'revision-'+i,revision:{generation:1},latest_run:{run_id:'run-'+i,status:'running'}})),edges:[]};
      host.innerHTML=FocusPortfolioMapView.render(manifest,'context-0',[],{presentation:'workbench'});
      FocusPortfolioMapView.bind(host);
      window.node=host.querySelector('[data-context-id="context-0"]');
      window.preview=node.querySelector('[data-context-preview]');
      window.peer=host.querySelector('[data-context-id="context-1"]');
      window.canvas=host.querySelector('.portfolio-map-canvas');
      window.scroll=host.querySelector('.portfolio-map-scroll');
      window.changedPeer=[];window.observer=new MutationObserver(changes=>changedPeer.push(...changes));observer.observe(peer,{attributes:true,childList:true,subtree:true,characterData:true});
      node.focus();scroll.scrollLeft=50;scroll.scrollTop=20;
      window.original={nodeStyle:node.getAttribute('style'),canvasStyle:canvas.getAttribute('style'),left:scroll.scrollLeft,top:scroll.scrollTop};
      window.patch=value=>FocusPortfolioMapView.patchPreviews(host,{'context-0':{run_id:'run-0',role:'Assistant',text:'',status:'streaming',...value}});
      window.measure=()=>{const n=node.getBoundingClientRect(),tail=preview.querySelector('.context-preview-tail').getBoundingClientRect(),text=preview.querySelector('.context-preview-text').getBoundingClientRect();return{width:n.width,height:n.height,tailHeight:tail.height,textTop:text.top,tailTop:tail.top,textBottom:text.bottom,tailBottom:tail.bottom,text:preview.querySelector('.context-preview-text').textContent}};
      patch({text:'第一行'});
    `);
    assert.equal(await run("host.querySelectorAll('[data-live-run-id]').length"), 8);
    let measure = await run("measure()");
    assert.equal(measure.width, 280); assert.equal(measure.height, 164); assert.equal(measure.tailHeight, 40); assert.ok(Math.abs(measure.textTop - measure.tailTop) < 1);
    for (const text of ["早前开头\n第二行\n倒数第二行\n最后一行", "中文持续更新".repeat(80) + " 最新尾部😀", "abcdefghijklmnopqrstuvwxyz".repeat(50) + " END", "<img src=x onerror=alert(1)>\n<script>visible only</script>"]) {
      await run(`patch({text:${JSON.stringify(text)}})`);
      measure = await run("measure()");
      assert.equal(measure.height, 164); assert.equal(measure.tailHeight, 40); assert.ok(Math.abs(measure.textBottom - measure.tailBottom) < 1); assert.equal(measure.text, text);
    }
    assert.equal(await run("preview.querySelectorAll('img,script').length"), 0);
    await run(`patch({text:'😀'.repeat(5000)+'TAIL'});`);
    assert.equal(await run("Array.from(preview.querySelector('.context-preview-text').textContent).length"), 4096);
    await run(`patch({role:'Tool',tool_name:'read_file',text:'',status:'tool_wait'});`);
    assert.equal(await run("preview.querySelector('.context-preview-role').textContent"), "Tool · read_file");
    assert.equal(await run("preview.querySelector('.context-preview-text').textContent"), "");
    assert.equal(await run("preview.dataset.previewStatus"), "tool_wait");
    await run(`patch(${JSON.stringify({role:"Tool",tool_name:"read_file",text:"文件中的最后两行\n文件结束",status:"disconnected",notice:"连接中断，保留最后真实输出"})});`);
    assert.equal(await run("preview.dataset.previewStatus"), "disconnected");
    const stable = await run(`(()=>{for(let i=0;i<240;i++)patch({text:${JSON.stringify("已读取工作区结构。\n现在检查桌面宠物入口 ")}+i});return {node:node===host.querySelector('[data-context-id="context-0"]'),preview:preview===node.querySelector('[data-context-preview]'),focus:document.activeElement===node,position:node.getAttribute('style')===original.nodeStyle,canvas:canvas.getAttribute('style')===original.canvasStyle,left:scroll.scrollLeft,top:scroll.scrollTop}})()`);
    assert.equal(stable.node, true); assert.equal(stable.preview, true); assert.equal(stable.focus, true); assert.equal(stable.position, true); assert.equal(stable.canvas, true);
    assert.equal(stable.left, await run("original.left")); assert.equal(stable.top, await run("original.top")); assert.equal(await run("changedPeer.length"), 0);
    const beforeWrongRun = await run("preview.textContent");
    await run(`patch({run_id:'late-run',text:'wrong run'});`); assert.equal(await run("preview.textContent"), beforeWrongRun);
    await run(`FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench'});`);
    assert.equal(await run("preview===node.querySelector('[data-context-preview]')"), true); assert.equal(await run("preview.textContent"), beforeWrongRun);
    await run(`manifest.nodes[0].title='运行中修改标题';FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench'});`);
    assert.equal(await run("preview===node.querySelector('[data-context-preview]') && document.activeElement===node"), true);
    for (const factor of [0.8, 2]) {
      await run(`canvas.style.zoom=${factor}`); measure = await run("measure()");
      assert.ok(Math.abs(measure.tailHeight - 40 * factor) < 1); assert.ok(Math.abs(measure.textBottom - measure.tailBottom) < 1);
    }
    await run(`manifest.nodes[0].title='你好';FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench'});canvas.style.zoom=1;patch({text:${JSON.stringify("我先读取工作区结构，确认桌面宠物的入口。\n接下来检查已有的交互实现。")}});scroll.scrollLeft=0;scroll.scrollTop=0;`);
    const output = process.env.FOCUS_PREVIEW_EVIDENCE;
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      for (const [name, value] of [["assistant", { role: "Assistant", text: "我先读取工作区结构，确认桌面宠物的入口。\n接下来检查已有的交互实现。", status: "streaming" }], ["tool-wait", { role: "Tool", tool_name: "read_file", text: "", status: "tool_wait" }], ["tool-result", { role: "Tool", tool_name: "read_file", text: "已经找到已有交互模块。\n文件末尾：export { DesktopPet };", status: "streaming" }], ["disconnected", { role: "Assistant", text: "最后收到的真实内容。\n后续输出尚未到达。", status: "disconnected" }]]) {
        await run(`patch(${JSON.stringify(value)})`);
        await run("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
        await new Promise(resolve => setTimeout(resolve, 200));
        await new Promise(resolve => {
          const done = () => { clearTimeout(timer); win.webContents.off("paint", done); resolve(); };
          const timer = setTimeout(done, 1000);
          win.webContents.once("paint", done); win.webContents.invalidate();
        });
        const rect = await run("(()=>{const r=node.getBoundingClientRect();return{x:Math.floor(r.x)-16,y:Math.floor(r.y)-16,width:312,height:196}})()");
        fs.writeFileSync(path.join(output, name + ".png"), (await win.webContents.capturePage(rect)).toPNG());
      }
    }
    win.setContentSize(390, 740); measure = await run("measure()"); assert.equal(measure.height, 164); assert.ok(Math.abs(measure.textBottom - measure.tailBottom) < 1);
    win.webContents.setZoomFactor(2);
    await new Promise(resolve => setTimeout(resolve, 40));
    measure = await run("measure()"); assert.equal(measure.height, 164); assert.ok(Math.abs(measure.textBottom - measure.tailBottom) < 1);
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{name:"forced-colors",value:"active"},{name:"prefers-reduced-motion",value:"reduce"},{name:"prefers-reduced-transparency",value:"reduce"}] });
    assert.equal(await run("getComputedStyle(node).backdropFilter"), "none");
    assert.equal(await run("getComputedStyle(preview).color"), await run("getComputedStyle(node).color"));
    assert.equal(await run("preview.getAnimations({subtree:true}).length"), 0);
    win.webContents.debugger.detach();
    win.webContents.setZoomFactor(1);
    await run(`FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench',selectedRevisionId:'history'});`);
    assert.equal(await run("node.querySelector('[data-context-preview]')===null && !node.hasAttribute('data-live-run-id')"), true);
    await run(`FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench'});patch({status:'ended'});`);
    assert.equal(await run("node.querySelector('[data-context-preview]').hidden"), true);
    await run(`manifest.nodes[0].latest_run.status='success';FocusPortfolioMapView.reconcile(host,manifest,'context-0',[],{presentation:'workbench'});`);
    assert.equal(await run("node.querySelector('[data-context-preview]')===null"), true);
    console.log("PASS running Context card: 8 live cards, visual tail, safe text, stable DOM/focus/viewport, role/status, scale and history");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
