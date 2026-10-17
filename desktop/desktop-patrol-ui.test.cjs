/*
 * 本文件对外提供生产机器人/面板页面的隔离 Electron UI 验收及可选截图。
 * 输入为真实宿主/preload/页面和受控工作区 HTTP/SSE；输出为显式发送、跨工作区草稿、回执/结果与真实窗口截图断言。
 * 工作流为启动临时 userData 与同源 fixture，经 DOM 拖放和真实 IPC 操作页面，捕获界面后关闭测试实例；
 * DOM 模拟拖放/输入法不代表 Explorer、操作系统输入法或真实模型验收。示例：node --test desktop/desktop-patrol-ui.test.cjs。
 * FOCUS_PATROL_NATIVE_PREVIEW=1 可保持真实 GPU 窗口供 Windows 合成验收；仅使用隔离 fixture，临时底色窗口作为浮层的所有者。
 * FOCUS_PATROL_NATIVE_DARK=1 使用深色验收底色。该手工模式不自动退出，不用于 node --test。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

if (!process.versions.electron) {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  test("production desktop Patrol keeps preview, explicit send and scoped feedback in real Electron", { timeout: 60000 }, async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-ui-"));
    const env = { ...process.env, FOCUS_PATROL_UI_USER_DATA: userData };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], { windowsHide: true, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", value => { stdout += value; });
    child.stderr.on("data", value => { stderr += value; });
    const timer = setTimeout(() => child.kill(), 55000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, `${stdout}\n${stderr}`);
      assert.match(stdout, /PATROL_UI_PASS/);
      console.log(stdout.trim());
    } finally {
      clearTimeout(timer);
      assert.ok(path.resolve(userData).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(userData, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  });
} else {
  const electron = require("electron");
  const { app, BrowserWindow, ipcMain } = electron;
  const http = require("node:http");
  const { DesktopPatrolHost } = require("./desktop-patrol-host.cjs");
  const { setTimeout: delay } = require("node:timers/promises");
  const directory = process.env.FOCUS_PATROL_UI_USER_DATA || fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-ui-"));
  app.setPath("userData", directory);
  if (process.platform !== "win32" && !process.env.FOCUS_PATROL_NATIVE_PREVIEW) {
    app.disableHardwareAcceleration();
    app.commandLine.appendSwitch("disable-gpu");
  }
  app.on("window-all-closed", () => {});
  const limits = { general_file_bytes: 1024 * 1024, original_image_bytes: 256 * 1024, run_material_count: 8 };
  const budgetCatalog = require("./workspace-patrol-budget-fixture.cjs"), grantChanges = [];
  let authorityRevision = 1, activeBudget = { ...budgetCatalog.presets.long.budgets };
  const session = "isolated-patrol-ui-fixture";
  const history = new Map(), streams = new Map(), requests = [], uploads = [], errors = [];
  let host, server, workbench, selectedPath = "C:\\fixture\\workspace-a", completed = false, dropReceipt = false, hiddenSubmission = null;
  const fileRef = {fact_id:"file-fixed",revision:2,result_key:"b".repeat(64),slot_id:"slot-a",path:"src/semantic_indexer.py",sha256:"a".repeat(64)};
  let fileReadDelay = 0, unavailableVersion = false, failWorkspaceInputs = 0, longHistory = false, failSubmission = 0;
  const submissionAttempts = [];
  let liveSequence = 1, answerRevision = "revision-answer", progressRevision = 1, statusOverride = null;
  const conversationReads = [];
  const workspace = id => ({ workspace_id: id, display_name: id === "workspace-a" ? "设计工作区 A" : "验证工作区 B", path: `C:\\fixture\\${id}` });
  const entity = (id, state) => ({ entity_id: id, revision: 1, updated_sequence: 1, state });
  const run = (win, script) => win.webContents.executeJavaScript(script);
  async function waitFor(win, expression, message) {
    for (let i = 0; i < 120; i++) {
      if (await run(win, expression)) return;
      await delay(40);
    }
    throw new Error(`${message}: ${await run(win, "document.body.innerText")}\n${errors.join("\n")}`);
  }
  async function capture(win, name) {
    if (!process.env.FOCUS_PATROL_CAPTURE_DIR) return;
    await run(win,"(async () => { await document.fonts.ready; await new Promise(resolve => requestAnimationFrame(resolve)); await Promise.all(document.getAnimations().filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished.catch(() => {}))); await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); })()");
    for (let i=0; i<100 && host?._resizeTimer; i++) await delay(16);
    await run(win,"new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    fs.mkdirSync(process.env.FOCUS_PATROL_CAPTURE_DIR, { recursive: true });
    const target = path.join(process.env.FOCUS_PATROL_CAPTURE_DIR, `${name}.png`);
    fs.writeFileSync(target, (await win.webContents.capturePage()).toPNG());
    fs.writeFileSync(target.replace(/\.png$/,'.json'),JSON.stringify(await run(win,"({dpr:devicePixelRatio,width:innerWidth,height:innerHeight,rects:Object.fromEntries(['#instruction','#send','.panel-shell','.panel-menu-content','#version-picker','#version-detail'].map(s=>[s,document.querySelector(s).getBoundingClientRect().toJSON()]))})"),null,2));
    console.log(`PATROL_UI_SCREENSHOT ${target}`);
  }
  function snapshot(loopId) {
    return { loop_id: loopId, last_sequence: liveSequence,
      loop: entity(loopId, { status: statusOverride || (completed ? "completed" : "running"), interaction_mode: "workspace_patrol", waiting_reason: null,
        authority_revision: authorityRevision, created_at: new Date(Date.now() - 600000).toISOString(),
        grant: { status: "active", budgets: activeBudget }, usage: { model_calls: 40, input_tokens: 100000, output_tokens: 5000, rounds: 5, retries: 0 } }),
      runs: completed ? { "run-answer": entity("run-answer", { run_id: "run-answer", context_id: "context-answer", status: "completed" }) } : {},
      contexts: completed ? { "context-answer": entity("context-answer", { context_id: "context-answer", title: "截图问题", current_revision_id: answerRevision }) } : {},
      task_progress: { ...entity(loopId, { generation: 0, document: { items: progressRevision > 1 ? [{ state: "completed", support: "supported" }] : [] } }), revision: progressRevision },
    };
  }
  function json(response, value, status = 200) {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(value));
  }
  async function route(request, response) {
    const url = new URL(request.url, "http://fixture.local");
    if (!url.pathname.startsWith("/desktop/api/")) {
      if (url.pathname === "/desktop/") return response.end("<!doctype html><title>工作台 fixture</title><body>工作台</body>");
      const relative = decodeURIComponent(url.pathname.replace(/^\/desktop\//, ""));
      const file = path.resolve(__dirname, relative);
      if (!file.startsWith(path.resolve(__dirname) + path.sep) || !fs.existsSync(file)) return json(response, { detail: `fixture missing ${url.pathname}` }, 404);
      const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" }[path.extname(file)] || "application/octet-stream";
      response.writeHead(200, { "Content-Type": type }); response.end(fs.readFileSync(file)); return;
    }
    assert.equal(request.headers["x-focus-session"], session);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const bytes = Buffer.concat(chunks);
    const body = bytes.length && request.headers["content-type"]?.startsWith("application/json") ? JSON.parse(bytes) : null;
    if (url.pathname.endsWith("/materials/limits")) return json(response, limits);
    if (url.pathname.endsWith("/facts")) { if (fileReadDelay) await delay(fileReadDelay); return json(response,{facts:[{fact_id:fileRef.fact_id,revision:fileRef.revision,kind:"artifact",file:fileRef},{fact_id:"file-grounding",revision:1,kind:"artifact",file:{...fileRef,path:"src/semantic_grounding.py",result_key:"d".repeat(64)}}],has_more:false,next_before:null}); }
    if (url.pathname.endsWith("/facts/file-fixed")) {
      assert.equal(url.searchParams.get("revision"),"2");
      if (unavailableVersion) return json(response,{file:{...fileRef,result_key:"c".repeat(64)}});
      return json(response,{file:{...fileRef,after:{available:true,text:"class ProtocolSafeRevisionSegmenter:\n    VERSION = 2"},before:{available:false,reason:"not_recorded"}}});
    }
    if (url.pathname.endsWith("/agent-loops/budget-presets")) return json(response, budgetCatalog);
    if (/\/agent-loops\/loop-[^/]+\/grant$/.test(url.pathname)) {
      grantChanges.push(body);
      assert.equal(body.expected_authority_revision, authorityRevision);
      authorityRevision++;
      activeBudget = { ...activeBudget, max_model_calls: activeBudget.max_model_calls + budgetCatalog.presets[body.preset].budgets.max_model_calls };
      return json(response, { authority_revision: authorityRevision });
    }
    if (/\/agent-loops\/loop-[^/]+$/.test(url.pathname)) return json(response, snapshot(url.pathname.split("/").at(-1)).loop.state);
    if (url.pathname === "/desktop/api/workspaces" && request.method === "POST") return json(response, workspace(body.path.split(/[\\/]/).at(-1)));
    const ws = url.pathname.match(/^\/desktop\/api\/workspaces\/([^/]+)$/);
    if (ws) return json(response, workspace(ws[1]));
    const upload = url.pathname.match(/^\/desktop\/api\/workspaces\/([^/]+)\/materials\/upload$/);
    if (upload) {
      uploads.push({ workspaceId: upload[1], submissionId: url.searchParams.get("submission_id"), itemId: url.searchParams.get("item_id"), bytes });
      return json(response, { material_id: `material-${uploads.length}`, sha256: "a".repeat(64) });
    }
    const input = url.pathname.match(/^\/desktop\/api\/agent-loops\/workspace\/([^/]+)(\/inputs)?$/);
    if (input) {
      const id = input[1];
      if (!input[2]) return json(response, history.has(id) ? { loop_id: `loop-${id}`, workspace_id: id } : null);
      if (request.method === "GET") {
        if (failWorkspaceInputs) { failWorkspaceInputs--; return json(response,{detail:"暂时读取失败"},503); }
        const items = (history.get(id) || []).filter(item => item.submission_id !== hiddenSubmission);
        return json(response, { items: longHistory ? [...items,...Array.from({length:20},(_,i)=>({...items[0],submission_id:`old-${i}`,intent_id:`old-intent-${i}`,content:`较早的输入 ${i} · 保持发送区可见`,created_at:new Date(Date.now()-1000*(i+1)).toISOString()}))] : items, next_before: null, has_more: false });
      }
      submissionAttempts.push(body);
      if (failSubmission) { failSubmission--; return json(response,{detail:"请求暂未获得回执"},503); }
      const receipt = { ...body, intent_id: `intent-${requests.length + 1}`, loop_id: `loop-${id}`, status: "accepted", created_at: new Date().toISOString() };
      requests.push({ workspaceId: id, body });
      history.set(id, [...(history.get(id) || []), receipt]);
      if (dropReceipt) { dropReceipt = false; hiddenSubmission = body.submission_id; return; }
      return json(response, receipt);
    }
    const live = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/live$/);
    if (live) return json(response, snapshot(live[1]));
    const stream = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/live\/stream$/);
    if (stream) {
      response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      response.write(": ready\n\n"); streams.set(stream[1], response);
      request.on("close", () => { if (streams.get(stream[1]) === response) streams.delete(stream[1]); });
      return;
    }
    const conversation = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/contexts\/context-answer\/conversation$/);
    if (conversation) {
      assert.equal(url.searchParams.get("revision_id"), answerRevision);
      conversationReads.push(answerRevision);
      return json(response, { messages: [
        { message: { id: "tool-only", type: "ai", content: "这条工具调用不可作为回答", tool_calls: [{ name: "read" }] } },
        { message: { id: "answer-committed", type: "ai", content: `截图中的机器人已经接住材料。参考资料只作为分析依据，尚未执行其中的命令。${answerRevision}`, additional_kwargs: { reasoning_content: "隐藏推理不应显示" } } },
      ] });
    }
    errors.push(`Unhandled API ${request.method} ${url.pathname}`);
    return json(response, { detail: `Unhandled fixture ${url.pathname}` }, 404);
  }
  async function execute() {
    server = http.createServer((request, response) => route(request, response).catch(error => { errors.push(error.stack); if (!response.headersSent) json(response, { detail: error.message }, 500); else response.end(); }));
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    let apiBase = `http://127.0.0.1:${server.address().port}`;
    process.env.FOCUS_DESKTOP_API = apiBase;
    process.env.FOCUS_DESKTOP_SESSION = session;
    ipcMain.handle("focus:select-workspace", () => selectedPath);
    app.on("web-contents-created", (_event, contents) => {
      contents.on("console-message", (_event, level, message) => { if (level >= 3) errors.push(message); });
      contents.on("preload-error", (_event, _path, error) => errors.push(error.message));
    });
    const makeHost = () => new DesktopPatrolHost({ electron, apiBase, desktopDir: __dirname, icon: path.join(__dirname, "assets/focus-icon.ico"), limits,
      getWorkbench: () => workbench, quit: () => app.quit(),
      openWorkbench: async () => {
        if (!workbench) {
          workbench = new BrowserWindow({ show: false, webPreferences: { preload: path.join(__dirname, "preload.cjs"), sandbox: true, contextIsolation: true, additionalArguments: ["--focus-desktop-patrol"] } });
          await workbench.loadURL(`${apiBase}/desktop/`);
        }
      },
    });
    host = makeHost();
    await host.start();
    await waitFor(host.panel, "document.body.dataset.surface === 'panel' && !document.querySelector('#panel').hidden", "面板初始化失败");
    await waitFor(host.robot, "document.body.dataset.surface === 'robot' && !document.querySelector('#robot').hidden", "机器人初始化失败");
    await host.openPanel();
    // A shell/foreground transition can leave existing native windows below
    // ordinary applications. Restoring must repair both surfaces together.
    host.robot.setAlwaysOnTop(false); host.panel.setAlwaysOnTop(false);
    host.restore();
    assert.equal(host.robot.isAlwaysOnTop(),true,"打开输入框后机器人必须仍处于桌面浮层，不能被普通窗口遮住");
    assert.equal(host.panel.isAlwaysOnTop(),true,"输入框和机器人必须保持同一浮层级别");
    await waitFor(host.panel,"document.querySelector('#send').disabled && document.querySelector('#status').textContent.length > 0","草稿初始化未完成");
    if (process.env.FOCUS_PATROL_NATIVE_PREVIEW) {
      const backdrop=new BrowserWindow({x:420,y:160,width:500,height:260,frame:false,resizable:false,backgroundColor:"#e8eef6",show:false,webPreferences:{sandbox:true,contextIsolation:true}});
      const backdropColor=process.env.FOCUS_PATROL_NATIVE_DARK ? "#253044" : "#e8eef6";
      await backdrop.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<title>Focus · Backdrop · Native QA</title><style>html,body{margin:0;height:100%;background:${backdropColor}}</style>`)}`);
      backdrop.showInactive();
      host.robot.setParentWindow(backdrop); host.panel.setParentWindow(backdrop);
      host.robot.setSkipTaskbar(false); host.panel.setSkipTaskbar(false);
      host.robot.setTitle("Focus · Robot · Native QA"); host.panel.setTitle("Focus · Panel · Native QA");
      host.robot.setBounds({...host.robot.getBounds(),x:760,y:240}); host.openPanel();
      console.log("PATROL_NATIVE_PREVIEW_READY");
      await new Promise(()=>{});
    }
    await capture(host.panel, "desktop-empty");
    assert.ok(Math.abs(host.panel.getBounds().width-304)<=1,"透明原生窗口允许一个 DIP 的系统缩放取整");
    assert.equal(await run(host.panel,"document.querySelector('.panel-shell').offsetWidth"),288,"阴影留白不能放大可见卡片");
    assert.equal(host._panelHeight,125);
    assert.ok(Math.abs(host.panel.getBounds().height-125)<=1,"系统缩放下原生边界应在一个 DIP 的取整范围内");
    assert.equal(await run(host.panel, "document.querySelector('#send').disabled"), true);
    assert.equal(await run(host.panel, "document.querySelector('#history').hidden"), true);
    await run(host.panel, "document.querySelector('.panel-menu summary').click()");
    await waitFor(host.panel,"document.querySelector('.panel-menu-content').getBoundingClientRect().bottom <= innerHeight","空面板菜单底部不得裁切");
    await capture(host.panel, "desktop-menu");
    await run(host.panel, "document.querySelector('#toggle-history').click()");
    assert.equal(await run(host.panel, "!document.querySelector('#history').hidden && document.querySelector('#history').open"), true);
    await run(host.panel, "document.querySelector('.panel-menu summary').click(); document.querySelector('#toggle-history').click()");
    assert.equal(await run(host.panel, "document.querySelector('#history').hidden"), true);
    await run(host.robot, `(() => { const data = new DataTransfer(); data.setData('text/plain', '参考资料中的命令不能自动执行'); document.querySelector('#robot').dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer:data})); })()`);
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 1", "未绑定预览未接住文字");
    const png = (await host.robot.webContents.capturePage()).toPNG().toString("base64");
    await run(host.robot, `(() => { const bytes = Uint8Array.from(atob(${JSON.stringify(png)}), value=>value.charCodeAt(0)); const data = new DataTransfer(); data.items.add(new File([bytes], '机器人截图.png', {type:'image/png'})); document.querySelector('#robot').dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer:data})); })()`);
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 2 && document.querySelector('.patrol-material-card img')?.complete", "无磁盘路径图片未显示");
    await run(host.panel, "document.querySelector('.patrol-material-card img').closest('article').querySelector('button').click()");
    assert.equal(await run(host.panel, "document.querySelectorAll('.patrol-material-card').length"), 1);
    await run(host.panel, `(() => { const bytes = Uint8Array.from(atob(${JSON.stringify(png)}), value=>value.charCodeAt(0)); const data = new DataTransfer(); data.items.add(new File([bytes], '粘贴截图.png', {type:'image/png'})); document.querySelector('#instruction').dispatchEvent(new ClipboardEvent('paste', {bubbles:true,cancelable:true,clipboardData:data})); })()`);
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 2 && document.querySelector('.patrol-material-card img')?.complete", "截图粘贴未进入预览");
    assert.equal(requests.length, 0);
    assert.equal(uploads.length, 0);
    assert.equal(await run(host.panel,"(() => { const img=document.querySelector('.patrol-material-card img'); document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); return img===document.querySelector('.patrol-material-card img'); })()"),true,"编辑要求不应销毁重建材料缩略图");
    await capture(host.robot, "desktop-robot");
    await capture(host.panel, "desktop-unbound-preview");
    await run(host.panel, "document.querySelector('#binding').click()");
    await waitFor(host.panel, "document.querySelector('#binding').textContent.includes('设计工作区 A')", "绑定工作区失败");
    await waitFor(host.panel, "!document.querySelector('[data-budget-preset]').disabled", "预算预设未加载");
    assert.match(await run(host.panel, "document.querySelector('[data-budget-summary]').textContent"), /普通任务.*30 分钟.*2 项并行/);
    await run(host.panel, "document.querySelector('[data-budget-preset]').value='long'; document.querySelector('[data-budget-preset]').dispatchEvent(new Event('change'))");
    assert.match(await run(host.panel, "document.querySelector('[data-budget-summary]').textContent"), /长任务.*120 分钟.*4 项并行/);
    assert.equal(requests.length, 0);
    await run(host.panel, `(() => { const input = document.querySelector('#instruction'); input.value = '解释这段参考资料'; input.dispatchEvent(new Event('input', {bubbles:true})); input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',bubbles:true})); input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',ctrlKey:true,isComposing:true,bubbles:true})); })()`);
    await delay(80);
    assert.equal(requests.length, 0, "Enter 和输入法组合阶段不能发送");
    await capture(host.panel, "desktop-bound-preview");
    await run(host.panel, "document.querySelector('#send').click()");
    await waitFor(host.panel, "document.querySelector('#receipt').textContent.includes('已受理') || document.querySelector('#history-items').textContent.includes('已受理')", "发送回执未显示");
    assert.equal(requests.length, 1);
    assert.equal(uploads.length, 2);
    assert.equal(requests[0].body.content, "解释这段参考资料");
    assert.equal(requests[0].body.budget_preset, "long");
    assert.equal(requests[0].body.attachments[0].source_kind, "text");
    assert.equal(requests[0].body.attachments[1].source_kind, "image");
    assert.doesNotMatch(await run(host.panel, "document.querySelector('#status').textContent"), /已完成/);
    await waitFor(host.panel, "!document.querySelector('[data-budget-increase]').hidden", "运行预算未显示");
    await run(host.panel, "document.querySelector('[data-patrol-budget] details').open=true; document.querySelector('[data-budget-preset]').value='normal'; document.querySelector('[data-budget-preset]').dispatchEvent(new Event('change')); document.querySelector('[data-budget-increase]').click()");
    await waitFor(host.panel, "document.querySelector('[data-budget-message]').textContent.includes('本次预算已增加')", "追加预算失败");
    assert.equal(grantChanges.length, 1);
    assert.equal(grantChanges[0].command, "increase_budget");
    assert.match(await run(host.panel, "document.querySelector('[data-budget-details]').textContent"), /已结算 40.*上限 500.*剩余 460/);
    await capture(host.panel, "desktop-running-budget");
    await run(host.panel, "document.querySelector('[data-patrol-budget] details').open=false");
    await run(host.panel, "document.querySelector('#instruction').value='A 的草稿'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
    selectedPath = "C:\\fixture\\workspace-b";
    await run(host.panel, "document.querySelector('#binding').click()");
    await waitFor(host.panel, "document.querySelector('#binding').textContent.includes('验证工作区 B')", "切换 B 失败");
    assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "");
    await run(host.panel, "document.querySelector('#instruction').value='B 的问题'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#instruction').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,bubbles:true,cancelable:true}))");
    for (let i = 0; i < 120 && requests.length < 2; i++) await delay(40);
    assert.equal(requests.length, 2);
    assert.equal(requests[1].workspaceId, "workspace-b");
    selectedPath = "C:\\fixture\\workspace-a";
    await run(host.panel, "document.querySelector('#binding').click()");
    await waitFor(host.panel, "document.querySelector('#instruction').value === 'A 的草稿'", "A 草稿未隔离恢复");
    await run(host.panel, "document.querySelector('#hide').click()");
    for (let i = 0; i < 60 && host.panel.isVisible(); i++) await delay(20);
    assert.equal(host.panel.isVisible(),false,"收起过渡结束后必须隐藏原生面板");
    await run(host.robot, "document.querySelector('#robot-open').click()");
    assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "A 的草稿");
    assert.equal(requests.length, 2);
    await capture(host.panel, "desktop-preserved-draft");
    // 真实 preload/同步 IPC/renderer 重建：本地草稿与材料不能依赖闭包或旧页面来源。
    await run(host.robot, `(() => { const data = new DataTransfer(); data.setData('text/plain', '刷新后保留的材料'); document.querySelector('#robot').dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer:data})); })()`);
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 1", "恢复材料未进入草稿");
    const streamBeforeReload = streams.get("loop-workspace-a");
    await new Promise(resolve => { host.panel.webContents.once("did-finish-load", resolve); host.panel.webContents.reload(); });
    await waitFor(host.panel, "document.querySelector('#instruction').value === 'A 的草稿' && document.querySelectorAll('.patrol-material-card').length === 1", "刷新后草稿或材料未恢复");
    assert.equal(requests.length, 2, "恢复不得自动发送");
    assert.equal(uploads.length, 2, "恢复不得自动上传");
    await run(host.panel, "document.querySelector('.patrol-material-card button').click()");
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 0", "恢复材料不能移除");
    completed = true;
    liveSequence++;
    for (let i = 0; i < 100 && (!streams.has("loop-workspace-a") || streams.get("loop-workspace-a") === streamBeforeReload); i++) await delay(40);
    assert.ok(streams.has("loop-workspace-a"));
    assert.notEqual(streams.get("loop-workspace-a"),streamBeforeReload,"刷新后的状态必须发送给新页面建立的连接");
    streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: 2 })}\n\n`);
    await waitFor(host.panel, "document.querySelector('#results').textContent.includes('截图中的机器人已经接住材料')", "已提交回答未显示");
    const resultText = await run(host.panel, "document.querySelector('#results').textContent");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-context-id]').dataset.contextId"), "context-answer");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-revision-id]').dataset.revisionId"), "revision-answer");
    assert.match(await run(host.panel, "document.querySelector('#results [data-context-id]').title"), /Context context-answer · Revision revision-answer/);
    assert.doesNotMatch(resultText, /隐藏推理|工具调用不可/);
    await capture(host.panel, "desktop-committed-answer");

    const readsBefore = conversationReads.length;
    await run(host.panel, "window.answerNode=document.querySelector('#answers article')");
    progressRevision++; liveSequence++;
    streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: liveSequence })}\n\n`);
    await waitFor(host.panel, "document.querySelector('#progress').textContent.includes('1 / 1')", "进度未独立刷新");
    assert.equal(conversationReads.length, readsBefore, "进度变化不能重新读取正文");
    assert.equal(await run(host.panel, "window.answerNode === document.querySelector('#answers article')"), true);

    host.panel.hide();
    await run(host.panel, "new Promise(resolve=>setTimeout(resolve,80))");
    await run(host.panel, "window.resultMutations=0; window.resultObserver=new MutationObserver(records=>{window.resultMutations+=records.length}); resultObserver.observe(document.querySelector('#results'),{subtree:true,childList:true,characterData:true})");
    answerRevision = "revision-answer-2"; statusOverride = "paused"; liveSequence++;
    streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: liveSequence })}\n\n`);
    await waitFor(host.robot, "document.querySelector('#robot-status').textContent === '已暂停'", "隐藏后机器人状态未更新");
    assert.equal(conversationReads.length, readsBefore, "真实隐藏窗口不能读取新 Revision 正文");
    assert.equal(await run(host.panel, "window.resultMutations"), 0, "隐藏后结果 DOM 不能更新");
    await host.openPanel();
    await waitFor(host.panel, "document.querySelector('#answers').textContent.includes('revision-answer-2')", "恢复时未读取最新版本");
    assert.equal(conversationReads.length, readsBefore + 1);
    host.panel.hide(); await host.openPanel();
    await run(host.panel, "new Promise(resolve=>setTimeout(resolve,80))");
    assert.equal(conversationReads.length, readsBefore + 1, "再次展开应复用 Revision 缓存");
    await run(host.panel, "window.resultObserver.disconnect()");
    await run(host.panel, "document.querySelector('#results [data-context-id]').click()");
    for (let i = 0; i < 120 && (!workbench || !workbench.webContents.getURL().endsWith("/desktop/")); i++) await delay(40);
    assert.ok(workbench);
    await run(workbench, "window.target=null; window.focusDesktop.patrol.onWorkbench(value=>window.target=value); window.focusDesktop.patrol.ready()");
    await waitFor(workbench, "Boolean(window.target)", "工作台未收到来源定位");
    assert.deepEqual(await run(workbench, "window.target"), { workspaceId: "workspace-a", taskId: "context-answer", revisionId: answerRevision });
    await host.openPanel();
    await run(host.panel,"document.querySelector('#reference-file').click()");
    await waitFor(host.panel,"document.querySelector('#version-candidates').textContent.includes('semantic_indexer.py')","文件版本候选未显示");
    await run(host.panel,"document.querySelector('#version-query').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}))");
    assert.equal(await run(host.panel,"document.activeElement === document.querySelector('#version-candidates button')"),true,"版本搜索应支持下方向键选择候选");
    await capture(host.panel,"desktop-version-picker");
    await run(host.panel,"document.querySelector('#version-query').value='missing'; document.querySelector('#version-query').dispatchEvent(new Event('input'))");
    assert.match(await run(host.panel,"document.querySelector('#version-candidates').textContent"),/没有匹配/);
    await run(host.panel,"document.querySelector('#version-query').value='semantic'; document.querySelector('#version-query').dispatchEvent(new Event('input')); document.querySelector('#version-candidates button').click()");
    await waitFor(host.panel,"document.querySelector('#file-refs').textContent.includes('semantic_indexer.py')","固定引用未加入草稿");
    assert.equal(await run(host.panel,"(() => { const chip=document.querySelector('#file-refs button'); chip.focus(); document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); return document.activeElement===chip; })()"),true,"草稿更新不应使文件引用丢失键盘焦点");
    await run(host.panel,"document.querySelector('#file-refs button').click()");
    await waitFor(host.panel,"document.querySelector('#version-body').textContent.includes('ProtocolSafeRevisionSegmenter')","固定版本详情未显示");
    await waitFor(host.panel,"innerWidth >= 580","详情未展开至左侧");
    assert.equal(await run(host.panel,"document.querySelector('.panel-shell').offsetWidth"),288);
    assert.equal(await run(host.panel,"document.querySelector('#version-picker').offsetWidth === 288 || document.querySelector('#version-picker').hidden"),true);
    await run(host.panel,"document.querySelector('[role=tab]').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true}))");
    assert.equal(await run(host.panel,"document.querySelectorAll('[role=tab]')[2].getAttribute('aria-selected')"),"true");
    await run(host.panel,"document.querySelector('[role=tab]').click()");
    await capture(host.panel,"desktop-version-detail");
    await run(host.panel,"document.querySelector('#close-version-detail').click()");
    await capture(host.panel,"desktop-file-reference");
    await run(host.panel,"document.querySelector('#send').click()");
    await waitFor(host.panel,"document.querySelector('#file-refs').children.length === 0","发送后版本引用未释放");
    for (let i = 0; i < 100 && requests.length < 3; i++) await delay(40);
    assert.deepEqual(requests[2].body.file_refs,[fileRef]);
    assert.equal(requests[2].body.content,"A 的草稿");
    await host.openPanel({workspaceId:"workspace-a",file_refs:[fileRef]});
    await waitFor(host.panel,"document.querySelector('#file-refs').textContent.includes('semantic_indexer.py')","工作台版本引用未交接");
    assert.equal(await run(host.panel,"document.querySelector('#send').disabled"),true,"仅文件版本仍需明确要求");
    unavailableVersion = true;
    await run(host.panel,"document.querySelector('#file-refs button').click()");
    await waitFor(host.panel,"document.querySelector('#version-body').textContent.includes('引用版本不可用')","版本失效未提示");
    assert.match(await run(host.panel,"document.querySelector('#file-refs').textContent"),/semantic_indexer/);
    await capture(host.panel,"desktop-version-unavailable");
    await run(host.panel,"document.querySelector('#close-version-detail').click()");
    await run(host.panel,"document.querySelector('#file-refs button:last-child').click()");
    await waitFor(host.panel,"document.querySelector('#file-refs').children.length === 0","引用未移除");
    failSubmission = 1;
    await run(host.panel,"document.querySelector('#instruction').value='未确认的输入'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click()");
    await waitFor(host.panel,"document.querySelector('#history-items').textContent.includes('按原内容重试')","失败输入必须提供原身份重试");
    await capture(host.panel,"desktop-send-unconfirmed");
    assert.equal(await run(host.panel,"(() => { const retry=document.querySelector('#history-items button'); retry.focus(); document.querySelector('#instruction').value='后续草稿'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); return document.activeElement===retry; })()"),true,"继续编辑不能重建重试按钮并抢走焦点");
    await run(host.panel,"document.querySelector('#history-items button').click()");
    await waitFor(host.panel,"!document.querySelector('#history-items').textContent.includes('按原内容重试')","原身份重试未受理");
    for (let i=0; i<100 && submissionAttempts.length<5; i++) await delay(20);
    assert.equal(submissionAttempts.at(-1).submission_id,submissionAttempts.at(-2).submission_id);
    assert.equal(await run(host.panel,"document.querySelector('#instruction').value"),"后续草稿","重试不得覆盖后续草稿");
    fileReadDelay = 200;
    await run(host.panel,"document.querySelector('#reference-file').click()");
    selectedPath = "C:\\fixture\\workspace-b";
    await run(host.panel,"document.querySelector('#binding').click()");
    await waitFor(host.panel,"document.querySelector('#binding').textContent.includes('验证工作区 B')","晚到读取工作区切换未完成");
    await delay(240);
    assert.equal(await run(host.panel,"document.querySelector('#version-picker').hidden"),true,"旧候选不得覆盖新工作区");
    failWorkspaceInputs = 1;
    longHistory = true;
    await host.openPanel({workspaceId:"workspace-a"});
    await waitFor(host.panel,"document.querySelector('#error').textContent.includes('暂时读取失败')","临时读取失败未呈现");
    await host.openPanel();
    await waitFor(host.panel,"document.querySelector('#error').hidden && document.querySelector('#results').textContent.includes('截图中的机器人')","机器人重新打开应恢复暂时失败的工作区读取");
    await run(host.panel,"document.querySelector('#instruction').value=Array(20).fill('长输入不会挡住操作区').join('\\n'); document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); if(document.querySelector('#history').hidden) document.querySelector('#toggle-history').click()");
    await waitFor(host.panel,"innerHeight >= 490","长内容窗口未扩展");
    assert.equal(await run(host.panel,"(() => { document.querySelector('#history-items article:last-child').scrollIntoView({block:'end'}); const b=document.querySelector('#send').getBoundingClientRect(); return b.top>=0 && b.bottom<=innerHeight; })()"),true,"滚动长历史时发送按钮必须保持可见");
    await host.openPanel({workspaceId:"workspace-a",file_refs:Array.from({length:32},(_,i)=>({...fileRef,fact_id:`file-${i}`,result_key:i.toString(16).padStart(64,'0'),path:`src/很长的目录/文件-${i}.py`}))});
    await waitFor(host.panel,"document.querySelector('#file-refs').children.length === 32","32项引用未完整保留");
    await run(host.panel,"document.querySelector('#reference-file').click()");
    await waitFor(host.panel,"document.querySelector('#version-candidates button')","压力场景候选未显示");
    assert.equal(await run(host.panel,"(() => { const b=document.querySelector('#send').getBoundingClientRect(),s=document.querySelector('.panel-shell').getBoundingClientRect(); return b.top>=s.top && b.bottom<=s.bottom && b.bottom<=innerHeight; })()"),true,"多引用同时打开选择器时不得挤掉发送区");
    await capture(host.panel,"desktop-stress-references");
    await run(host.panel,"document.querySelector('#close-versions').click(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))");
    await delay(30); await host.openPanel(); await delay(420);
    assert.equal(host.panel.isVisible(),true,"快速重新打开不能被上一次收起动画隐藏");
    assert.equal(await run(host.panel,"document.querySelector('#panel').inert"),false,"重新打开后输入区不能保持 inert");
    host.robot.setBounds({x:300,y:300,width:96,height:120});
    const movedBounds = host.robot.getBounds();
    host.robot.webContents.sendInputEvent({type:"mouseDown",x:30,y:30,globalX:movedBounds.x+30,globalY:movedBounds.y+30,button:"left",clickCount:1});
    await delay(30);
    host.robot.webContents.sendInputEvent({type:"mouseMove",x:60,y:50,globalX:movedBounds.x+60,globalY:movedBounds.y+50,button:"left"});
    await delay(80);
    host.robot.webContents.sendInputEvent({type:"mouseUp",x:60,y:50,button:"left",clickCount:1});
    await delay(30);
    assert.ok(Math.abs(host.robot.getBounds().x-(movedBounds.x+30)) <= 1,"系统调整机器人位置后继续拖动不能跳回旧位置");
    longHistory = false;
    await run(host.panel,"document.querySelectorAll('#file-refs button[aria-label^=移除]').forEach(button=>button.click())");
    const requestsBeforeLostReceipt = requests.length;
    dropReceipt = true;
    await run(host.panel, "document.querySelector('#instruction').value='服务端已受理但回执丢失'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click()");
    await waitFor(host.panel, "document.querySelector('#history-items').textContent.includes('发送中')", "未收到回执前发送状态错误");
    for (let i = 0; i < 120 && requests.length < requestsBeforeLostReceipt + 1; i++) await delay(40);
    assert.equal(requests.length, requestsBeforeLostReceipt + 1);
    await run(host.panel, "document.querySelector('#instruction').value='重启后继续编辑'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
    await run(host.robot, `(() => { const bytes = Uint8Array.from(atob(${JSON.stringify(png)}), value=>value.charCodeAt(0)); const data = new DataTransfer(); data.items.add(new File([bytes], '重启草稿.png', {type:'image/png'})); document.querySelector('#robot').dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer:data})); })()`);
    await waitFor(host.panel, "document.querySelectorAll('.patrol-material-card').length === 1", "重启前材料未保存");
    host.dispose(); workbench.destroy(); workbench = null;
    hiddenSubmission = null;
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    streams.clear();
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    apiBase = `http://127.0.0.1:${server.address().port}`;
    process.env.FOCUS_DESKTOP_API = apiBase;
    host = makeHost(); await host.start(); await host.openPanel();
    await waitFor(host.panel, "document.querySelector('#instruction').value === '重启后继续编辑' && document.querySelector('.patrol-material-card img')?.complete && document.querySelector('#history-items').textContent.includes('服务端已受理但回执丢失') && ![...document.querySelectorAll('#history-items small')].some(node => node.textContent.startsWith('未确认'))", "跨来源重启后未恢复草稿或核对回执");
    assert.equal(requests.length, requestsBeforeLostReceipt + 1, "服务端已受理的输入不能在重启后重发");
    assert.equal(uploads.length, 2, "重启恢复不能自动上传草稿附件");
    assert.deepEqual(errors, []);
    console.log("PATROL_UI_PASS preview-before-binding, pathless-image, screenshot-paste, remove, explicit-send, IME guard, CtrlEnter, workspace isolation, hide/restore, accepted-is-not-complete, committed-answer-source, independent-progress, hidden-refresh-budget, revision-cache, reload-recovery, new-port-recovery, lost-receipt-reconciliation");
  }
  app.whenReady().then(execute).then(() => cleanup(0)).catch(error => { console.error(error.stack || error); console.error(errors.join("\n")); cleanup(1); });
  function cleanup(code) {
    host?.dispose();
    if (workbench && !workbench.isDestroyed()) workbench.destroy();
    server?.closeAllConnections(); server?.close();
    app.exit(code);
  }
}
