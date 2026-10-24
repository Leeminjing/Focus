/*
 * 本文件对外提供生产机器人/面板页面的隔离 Electron UI 验收及可选截图。
 * 输入为真实宿主/preload/页面和受控工作区 HTTP/SSE；输出为显式发送、跨工作区草稿、回执/结果与真实窗口截图断言。
 * 工作流为启动临时 userData 与同源 fixture，经 DOM 拖放和真实 IPC 操作页面，捕获界面后关闭测试实例；
 * DOM 模拟拖放/输入法不代表 Explorer、操作系统输入法或真实模型验收。示例：node --test desktop/tests/integration/desktop-patrol-ui.test.cjs。
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
  for (const scaleFactor of [1,1.25,1.5]) test(`production desktop Patrol keeps input stable and feedback scoped in real Electron (${scaleFactor}x)`, { timeout: 60000 }, async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-ui-"));
    const env = { ...process.env, FOCUS_PATROL_UI_USER_DATA: userData };
    if (env.FOCUS_PATROL_CAPTURE_DIR) env.FOCUS_PATROL_CAPTURE_DIR = path.join(env.FOCUS_PATROL_CAPTURE_DIR, String(scaleFactor));
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [`--force-device-scale-factor=${scaleFactor}`,__filename], { windowsHide: true, env, stdio: ["ignore", "pipe", "pipe"] });
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
  const { DesktopPatrolHost } = require("../../src/main/desktop-patrol-host.cjs");
  const { registerDesktopPreferences } = require("../../src/main/desktop-preferences.cjs");
  const { setTimeout: delay } = require("node:timers/promises");
  const directory = process.env.FOCUS_PATROL_UI_USER_DATA || fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-ui-"));
  app.setPath("userData", directory);
  if (process.platform !== "win32" && !process.env.FOCUS_PATROL_NATIVE_PREVIEW) {
    app.disableHardwareAcceleration();
    app.commandLine.appendSwitch("disable-gpu");
  }
  app.on("window-all-closed", () => {});
  const limits = { general_file_bytes: 1024 * 1024, original_image_bytes: 256 * 1024, run_material_count: 8 };
  const budgetCatalog = require("../fixtures/workspace-patrol-budget-fixture.cjs"), grantChanges = [];
  let authorityRevision = 1, activeBudget = { ...budgetCatalog.presets.long.budgets };
  const session = "isolated-patrol-ui-fixture";
  const history = new Map(), streams = new Map(), requests = [], uploads = [], errors = [], workspaceReads = [];
  let previewPng = null;
  let host, server, workbench, selectedPath = "C:\\fixture\\workspace-a", completed = false, dropReceipt = false, hiddenSubmission = null;
  const fileRef = {fact_id:"file-fixed",revision:2,result_key:"b".repeat(64),slot_id:"slot-a",path:"src/semantic_indexer.py",sha256:"a".repeat(64)};
  const reportBytes = Buffer.from("%PDF-1.7\nFocus report\n\0binary");
  const reportPath = path.join(directory, "报告.pdf"), savedReport = path.join(directory, "报告副本.pdf");
  fs.mkdirSync(directory, { recursive: true }); fs.writeFileSync(reportPath, reportBytes);
  const reportFile = { fact_id:"report-file", revision:1, context_id:"context-answer", occurred_at:"2026-10-17T09:30:00Z", kind:"artifact",
    file:{slot_id:"slot-a",path:"报告.pdf",result_key:"report-current",sha256:require("node:crypto").createHash("sha256").update(reportBytes).digest("hex"),operation:"created"} };
  const nativeExits = [];
  let fileReadDelay = 0, unavailableVersion = false, failWorkspaceInputs = 0, longHistory = false, failSubmission = 0;
  let inputProcessingResult = null;
  const submissionAttempts = [];
  let liveSequence = 1, answerRevision = "revision-answer", progressRevision = 1, statusOverride = null, loopRevision = 1;
  const controlChanges = [];
  const restartRequests = [], currentLoops = new Map();
  let restartError = false, releaseRestartSnapshot = null, holdRestartSnapshot = false;
  const conversationReads = [], externalLinks = [], previewReads = [], sharedActions = [];
  const answerText = revision => `截图中的机器人已经接住材料。参考资料只作为分析依据，尚未执行其中的命令。${revision}\n\n## 处理结果\n\n**已完成检查**，可以继续阅读详细说明。\n\n${"正文按自然段阅读，代码与表格保留原有结构。\n\n".repeat(8)}\n\n\`\`\`js\nconst value = "<safe>";\n\`\`\`\n\n| 文件 | 状态 |\n| --- | --- |\n| result.md | 已生成 |\n\n[参考链接](https://example.com/result)\n\n[查看报告](<reports/中文 报告.md>)\n\n<script>window.answerInjected=true</script>`;
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
    await run(win,"new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    fs.mkdirSync(process.env.FOCUS_PATROL_CAPTURE_DIR, { recursive: true });
    const target = path.join(process.env.FOCUS_PATROL_CAPTURE_DIR, `${name}.png`);
    fs.writeFileSync(target, (await win.webContents.capturePage()).toPNG());
    fs.writeFileSync(target.replace(/\.png$/,'.json'),JSON.stringify(await run(win,"({dpr:devicePixelRatio,width:innerWidth,height:innerHeight,rects:Object.fromEntries(['#instruction','#send','.panel-shell','.panel-menu-content','#version-picker','#version-detail'].map(s=>[s,document.querySelector(s).getBoundingClientRect().toJSON()]))})"),null,2));
    console.log(`PATROL_UI_SCREENSHOT ${target}`);
  }
  async function verifyInputSizing(resizeRequests) {
    const writes = [], setBounds = host.panel.setBounds.bind(host.panel);
    host.panel.setBounds = bounds => { writes.push(bounds); return setBounds(bounds); };
    const settle = () => run(host.panel,"new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))))");
    try {
      await run(host.panel,"(async () => { await document.fonts.ready; await Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{}))); document.querySelector('#instruction').focus(); })()");
      await settle();
      const initial = host.panel.getBounds(), first = writes.length, firstRequest = resizeRequests.length;
      const geometry = () => run(host.panel,"JSON.stringify(['#instruction','#send','.panel-shell'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [r.x,r.y,r.width,r.height]}))");
      const initialGeometry = await geometry();
      for (let i=0;i<12;i++) { await host.panel.webContents.insertText("a"); await settle(); }
      assert.equal(writes.length,first,"未换行输入不得移动或缩放原生窗口");
      assert.equal(resizeRequests.length,firstRequest,"未换行输入不得重复发送尺寸 IPC");
      assert.deepEqual(host.panel.getBounds(),initial);
      assert.equal(await geometry(),initialGeometry,"同行输入时输入框、发送按钮和卡片都应保持原位");
      let changes = 0;
      for (let i=0;i<12;i++) {
        const before = writes.length, beforeRequest = resizeRequests.length;
        await host.panel.webContents.insertText("测试多行文字输入窗口尺寸变化。");
        await settle();
        assert.ok(writes.length-before <= 1,"一次输入布局变化不能产生逐帧原生窗口动画");
        assert.ok(resizeRequests.length-beforeRequest <= 1,"同一目标只能发送一次尺寸 IPC");
        changes += writes.length-before;
      }
      assert.ok(changes>0,"长输入必须真实触发窗口扩展");
      assert.equal(await run(host.panel,"document.querySelector('#instruction').offsetHeight"),108);
      const count = writes.length, requestCount = resizeRequests.length, final = host.panel.getBounds();
      await delay(450);
      assert.equal(writes.length,count,"停止输入后不得继续调整原生窗口");
      assert.equal(resizeRequests.length,requestCount,"停止输入后尺寸观察必须收敛");
      assert.deepEqual(host.panel.getBounds(),final);
      assert.equal(await run(host.panel,"document.activeElement.id"),"instruction","输入期间不能丢失焦点");
      await capture(host.panel,"desktop-typing-stable");
      await run(host.panel,"document.querySelector('#instruction').value=''; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
      await settle();
      assert.ok(Math.abs(host.panel.getBounds().height-125)<=1,"清空后应收回基础高度");
      console.log(`PATROL_SIZING_PASS scale=${electron.screen.getPrimaryDisplay().scaleFactor} same-line-writes=0 multiline-writes=${changes} settled-writes=0`);
    } finally { host.panel.setBounds = setBounds; }
  }
  function snapshot(loopId) {
    return { loop_id: loopId, last_sequence: liveSequence,
      loop: entity(loopId, { status: statusOverride || (completed ? "waiting_user" : "running"), revision: loopRevision, interaction_mode: "workspace_patrol", waiting_reason: completed ? "awaiting_input" : null,
        current_round_id: "delivery-round", goal_revision: 1,
        final_result: completed ? { summary: "完成截图分析并生成报告", round_id: "delivery-round", goal_revision: 1,
          verification: { verification_id: "accepted-delivery", conclusion: "satisfied", criteria: [
            { check_id: "report", status: "satisfied", explanation: "报告内容检查通过" },
            { check_id: "optional", status: "unknown", explanation: "可选平台尚未验证" },
          ], unresolved: ["optional"] }, final_path: [{ context_id: "context-answer", revision_id: "revision-answer", title: "分析报告" }] } : {},
        authority_revision: authorityRevision, created_at: new Date(Date.now() - 600000).toISOString(),
        grant: { status: "active", budgets: activeBudget }, usage: { model_calls: 40, input_tokens: 100000, output_tokens: 5000, rounds: 5, retries: 0 } }),
      runs: completed ? { "run-answer": entity("run-answer", { run_id: "run-answer", context_id: "context-answer", status: "completed" }) } : {},
      interventions: inputProcessingResult ? Object.fromEntries((history.get("workspace-a") || []).map(row => [row.intent_id,
        { ...entity(row.intent_id, { state: "addressed" }), revision: loopRevision }])) : {},
      contexts: completed ? { "context-answer": entity("context-answer", { context_id: "context-answer", title: "截图问题", current_revision_id: answerRevision }) } : {},
      task_progress: { ...entity(loopId, { generation: 0, document: { items: progressRevision > 1 ? [{ state: "completed", support: "supported" }] : [] } }), revision: progressRevision },
      facts: completed ? { [fileRef.fact_id]: entity(fileRef.fact_id, { ...fileRef, kind:"artifact", file:fileRef }), "report-file": entity("report-file", reportFile) } : {},
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
      const file = path.resolve(__dirname, "../../src/renderer", relative);
      if (!file.startsWith(path.resolve(__dirname, "../../src/renderer") + path.sep) || !fs.existsSync(file)) return json(response, { detail: `fixture missing ${url.pathname}` }, 404);
      const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" }[path.extname(file)] || "application/octet-stream";
      response.writeHead(200, { "Content-Type": type }); response.end(fs.readFileSync(file)); return;
    }
    assert.equal(request.headers["x-focus-session"], session);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const bytes = Buffer.concat(chunks);
    const body = bytes.length && request.headers["content-type"]?.startsWith("application/json") ? JSON.parse(bytes) : null;
    if (url.pathname.endsWith("/materials/limits")) return json(response, limits);
    if (url.pathname.endsWith("/facts")) { if (fileReadDelay) await delay(fileReadDelay); return json(response,{facts:[{fact_id:fileRef.fact_id,revision:fileRef.revision,kind:"artifact",file:fileRef},{fact_id:"file-grounding",revision:1,kind:"artifact",file:{...fileRef,path:"src/semantic_grounding.py",result_key:"d".repeat(64)}},...(url.searchParams.get("limit") === "200" ? [reportFile,{...reportFile,fact_id:"report-old",occurred_at:"2026-10-16T09:30:00Z",file:{...reportFile.file,result_key:"report-old"}}] : [])],has_more:false,next_before:null}); }
    if (/\/facts\/report-(file|old)$/.test(url.pathname)) {
      const old = url.pathname.endsWith("report-old");
      return json(response,{file:{...reportFile.file,result_key:old ? "report-old" : "report-current",after:old ? {available:true,text:"报告初稿"} : {available:false,reason:"binary"},before:{available:false,reason:"binary"}}});
    }
    if (url.pathname === "/desktop/api/files/pdf-page") {
      previewReads.push(body);
      assert.deepEqual(body, {loop_id:"loop-workspace-a",fact_id:"report-file",revision:1});
      return json(response, {image:previewPng.toString("base64"),page_count:2});
    }
    if (url.pathname === "/desktop/api/files/content") {
      previewReads.push(body);
      assert.deepEqual(body, {task_id:"context-answer",link:{path:"reports/中文 报告.md",workspace_id:"workspace-a",run_id:"run-answer",slot_id:"slot-a",view:"current"}});
      response.writeHead(200, {"Content-Type":"application/octet-stream"});
      return response.end("# 正确执行位置的报告\n\n共享阅读器内容");
    }
    if (/\/inputs\/[^/]+\/attachments\/[^/]+\/content$/.test(url.pathname)) {
      const isImage = url.pathname.includes("material-2/");
      response.writeHead(200, { "Content-Type": isImage ? "image/png" : "text/plain" });
      return response.end(isImage ? previewPng : "冻结的参考文字");
    }
    if (url.pathname.endsWith("/facts/file-fixed")) {
      assert.equal(url.searchParams.get("revision"),"2");
      if (unavailableVersion) return json(response,{file:{...fileRef,result_key:"c".repeat(64)}});
      return json(response,{file:{...fileRef,after:{available:true,text:"class ProtocolSafeRevisionSegmenter:\n    VERSION = 2"},before:{available:false,reason:"not_recorded"}}});
    }
    if (url.pathname.endsWith("/agent-loops/budget-presets")) return json(response, budgetCatalog);
    const control = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/control$/);
    if (control && request.method === "POST") {
      controlChanges.push({ loopId: control[1], command: body.command });
      return json(response, { loop_id: control[1], revision: ++loopRevision });
    }
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
    const restart = url.pathname.match(/^\/desktop\/api\/agent-loops\/workspace\/([^/]+)\/restart$/);
    if (restart) {
      restartRequests.push(restart[1]);
      if (restartError) return json(response, { detail: "前序执行尚未安全结算" }, 409);
      const loopId = `loop-restarted-${restartRequests.length}`;
      currentLoops.set(restart[1], loopId); statusOverride = "running";
      return json(response, { loop_id: loopId, status: "running" });
    }
    const input = url.pathname.match(/^\/desktop\/api\/agent-loops\/workspace\/([^/]+)(\/inputs)?$/);
    if (input) {
      const id = input[1];
      if (!input[2]) { workspaceReads.push(id); return json(response, history.has(id) ? { loop_id: currentLoops.get(id) || `loop-${id}`, workspace_id: id } : null); }
      if (request.method === "GET") {
        if (failWorkspaceInputs) { failWorkspaceInputs--; return json(response,{detail:"暂时读取失败"},503); }
        const requestedIds = url.searchParams.getAll("intent_ids");
        const items = (history.get(id) || []).filter(item => item.submission_id !== hiddenSubmission && (!requestedIds.length || requestedIds.includes(item.intent_id)))
          .map((item,index) => inputProcessingResult && index === 0 ? {...item,processing_state:'addressed',processing_result:inputProcessingResult} : item);
        return json(response, { items: longHistory ? [...items,...Array.from({length:20},(_,i)=>({...items[0],submission_id:`old-${i}`,intent_id:`old-intent-${i}`,content:`较早的输入 ${i} · 保持发送区可见`,created_at:new Date(Date.now()-1000*(i+1)).toISOString()}))] : items, next_before: null, has_more: false });
      }
      submissionAttempts.push(body);
      if (failSubmission) { failSubmission--; return json(response,{detail:"请求暂未获得回执"},503); }
      const receipt = { ...body, ...(body.attachments ? {attachments:body.attachments.map(item => ({...item,
        relative_path:`.focus/attachments/${item.source_kind === "image" ? "粘贴截图.png" : "参考文字.txt"}`,
        material_kind:item.source_kind === "image" ? "image" : "text",digest:item.sha256}))} : {}),
        intent_id: `intent-${requests.length + 1}`, loop_id: currentLoops.get(id) || `loop-${id}`, status: "accepted", created_at: new Date().toISOString() };
      requests.push({ workspaceId: id, body });
      history.set(id, [...(history.get(id) || []), receipt]);
      if (dropReceipt) { dropReceipt = false; hiddenSubmission = body.submission_id; return; }
      return json(response, receipt);
    }
    const live = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/live$/);
    if (live) {
      if (holdRestartSnapshot && live[1].startsWith("loop-restarted-")) await new Promise(resolve => { releaseRestartSnapshot = resolve; });
      return json(response, snapshot(live[1]));
    }
    const liveHistory = url.pathname.match(/^\/desktop\/api\/agent-loops\/([^/]+)\/live\/history$/);
    if (liveHistory) return json(response, { loop_id: liveHistory[1], last_sequence: liveSequence, authority_revision: authorityRevision,
      items: [], next_before_sequence: null, minimum_sequence: 1, truncated: false });
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
        { message: { id: "answer-committed", type: "ai", content: answerText(answerRevision), additional_kwargs: { reasoning_content: "隐藏推理不应显示" } },
          file_context:{task_id:"context-answer",workspace_id:"workspace-a",run_id:"run-answer",slot_id:"slot-a"} },
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
    const exits = require("../../src/main/artifact-files.cjs").createArtifactFiles({
      resolve: async reference => { assert.equal(reference.factId,"report-file"); return {root_path:directory,path:reportPath,sha256:reportFile.file.sha256}; },
      shell: { openPath: async target => { nativeExits.push(["open",target]); return ""; }, showItemInFolder: target => nativeExits.push(["reveal",target]) },
      dialog: { showSaveDialog: async () => ({filePath:savedReport}) },
    });
    for (const [channel,operation] of [["open","open"],["reveal","reveal"],["save-as","saveAs"]]) ipcMain.handle(`focus:file-${channel}`, async (event,reference) => {
      assert.ok(host.accepts(event)); return exits[operation](reference);
    });
    ipcMain.handle("focus:select-workspace", () => selectedPath);
    ipcMain.handle("focus:gateway-status", () => ({ status: "ready", error: null, revision: 1 }));
    ipcMain.handle("focus:open-external", (_event, url) => { externalLinks.push(url); return true; });
    ipcMain.handle("focus:file-action", (event, request) => { assert.ok(host.accepts(event)); sharedActions.push(request); return true; });
    registerDesktopPreferences(ipcMain,{directory:() => directory,authorize:event => { assert.ok(host.accepts(event)); }});
    const resizeRequests = [], register = ipcMain.handle.bind(ipcMain);
    ipcMain.handle = (channel,handler) => register(channel,channel === "focus:patrol-resize-panel" ? (event,size) => { resizeRequests.push(size); return handler(event,size); } : handler);
    app.on("web-contents-created", (_event, contents) => {
      contents.on("console-message", (_event, level, message) => { if (level >= 3) errors.push(message); });
      contents.on("preload-error", (_event, _path, error) => errors.push(error.message));
    });
    const makeHost = () => new DesktopPatrolHost({ electron, apiBase, desktopDir: path.resolve(__dirname, "../.."), icon: path.join(__dirname, "../../src/renderer/assets/focus-icon.ico"), limits,
      getWorkbench: () => workbench, quit: () => app.quit(),
      openWorkbench: async () => {
        if (!workbench) {
          workbench = new BrowserWindow({ show: false, webPreferences: { preload: path.join(__dirname, "../../src/preload/preload.cjs"), sandbox: true, contextIsolation: true, additionalArguments: ["--focus-desktop-patrol"] } });
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
    await host.flushRecovery();
    let draftSaves = 0, synchronousSyncs = 0;
    const save = host._recovery.save.bind(host._recovery), fsyncSync = fs.fsyncSync;
    host._recovery.save = state => { draftSaves++; return save(state); };
    fs.fsyncSync = (...args) => { synchronousSyncs++; return fsyncSync(...args); };
    try {
      await run(host.panel, `(() => { const input=document.querySelector('#instruction'); for(let i=0;i<100;i++){ input.value='draft-'+i; input.dispatchEvent(new Event('input',{bubbles:true})); } })()`);
      assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "draft-99");
      await host.flushRecovery();
      assert.equal(synchronousSyncs, 0, "真实输入/preload/IPC 路径不能同步刷盘");
      assert.equal(draftSaves, 1, "100 次编辑应合并保存");
      assert.equal(host._recovery.read().state.workspaces[0][1].draft.content, "draft-99");
    } finally { host._recovery.save = save; fs.fsyncSync = fsyncSync; }
    await run(host.panel, "document.querySelector('#instruction').value=''; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
    await host.flushRecovery();
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
    await verifyInputSizing(resizeRequests);
    await capture(host.panel, "desktop-empty");
    for (let i = 0; i < 100 && host._panelHeight == null; i++) await delay(20);
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
    previewPng = Buffer.from(png,"base64");
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
    assert.equal(await run(host.panel, "document.querySelector('[data-patrol-budget], #task-budget')"), null);
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
    assert.equal(requests[0].body.budget_preset, undefined);
    assert.equal(requests[0].body.attachments[0].source_kind, "text");
    assert.equal(requests[0].body.attachments[1].source_kind, "image");
    await run(host.panel,"document.querySelector('#toggle-history').click()");
    inputProcessingResult = {disposition:'decision',explanation:'已调整优先级：先定位导入失败。下一步验证错误原因。',mission_revision:3,decision_id:'input-decision',round_id:'input-round',observation_id:'input-observation',observation_generation:2,rationale:'导入失败阻塞后续工作',evidence:[{fact_id:'import-error',revision:4}],related_cancellations:[{state:'cancel_requested',target_kind:'run',target_id:'ui-run',round_id:'old-round',reason:'先解决导入失败'}]};
    for (let attempt=0; attempt<100 && !streams.has('loop-workspace-a'); attempt++) await delay(20);
    loopRevision++; liveSequence++;
    streams.get('loop-workspace-a').write(`data: ${JSON.stringify({status:'resync_required',last_sequence:liveSequence})}\n\n`);
    await waitFor(host.panel, "document.querySelector('[data-patrol-input-result] summary')?.textContent.includes('已请求停止')", "每条输入必须显示真实的处理反馈");
    await run(host.panel,"document.querySelector('[data-patrol-input-result] summary').click(); document.querySelector('[data-patrol-input-result] summary').focus()");
    assert.match(await run(host.panel,"document.querySelector('[data-patrol-input-result]').textContent"),/input-decision.*input-observation.*import-error/s);
    await capture(host.panel,'desktop-input-processing-requested');
    inputProcessingResult = {...inputProcessingResult,related_cancellations:inputProcessingResult.related_cancellations.map(item=>({...item,state:'stopped'}))};
    loopRevision++; liveSequence++;
    streams.get('loop-workspace-a').write(`data: ${JSON.stringify({status:'resync_required',last_sequence:liveSequence})}\n\n`);
    await waitFor(host.panel,"document.querySelector('[data-patrol-input-result] summary')?.textContent.includes('同次决策：已停止')","清理结束后反馈必须更新");
    assert.equal(await run(host.panel,"document.querySelector('[data-patrol-input-result]').open && document.activeElement.matches('[data-patrol-input-result] > summary')"),true,"反馈更新必须保留已展开来源和键盘焦点");
    await run(host.panel,"document.querySelector('[data-patrol-input-result]').open=false; document.querySelector('[data-patrol-history-attachment=\"1\"]').scrollIntoView({block:'center'})");
    await waitFor(host.panel,"document.querySelector('#history-items summary img')?.naturalWidth > 0","已发送图片应保留缩略图");
    assert.match(await run(host.panel,"document.querySelector('#history-items').textContent"),/粘贴截图.png.*版本 aaaaaaaa/s);
    await capture(host.panel,"desktop-sent-attachments");
    await run(host.panel,"document.querySelector('[data-patrol-history-attachment=\"0\"]').open=true; document.querySelector('[data-patrol-history-attachment=\"1\"]').open=true");
    await waitFor(host.panel,"document.querySelector('[data-patrol-history-preview] pre')?.textContent === '冻结的参考文字' && document.querySelector('[data-patrol-history-preview] img')?.naturalWidth > 0","历史附件应能展开读取原版本");
    assert.equal(await run(host.panel,"(() => { const s=document.querySelector('.panel-scroll'); return s.scrollWidth<=s.clientWidth+1; })()"),true,"展开的附件不得撑出横向滚动");
    await run(host.panel,"document.querySelector('[data-patrol-history-attachment=\"0\"]').open=false; document.querySelector('[data-patrol-history-attachment=\"1\"]').scrollIntoView({block:'start'})");
    await capture(host.panel,"desktop-sent-attachment-preview");
    await run(host.panel,"document.querySelector('#toggle-history').click()");
    assert.doesNotMatch(await run(host.panel, "document.querySelector('#status').textContent"), /已完成/);
    assert.equal(await run(host.panel, "document.querySelector('[data-patrol-budget], [data-budget-preset], [data-budget-increase]')"), null);
    assert.equal(grantChanges.length, 0);
    await capture(host.panel, "desktop-running");
    // Exercise the production API and Live connection, holding back the state
    // update until HTTP acceptance has been observed by the renderer.
    for (const [command, target, label] of [["pause", "paused", "继续"], ["resume", "running", "暂停"], ["stop", "stopped", "暂停"]]) {
      if (command === "stop") {
        await run(host.panel, "document.querySelector('.panel-menu summary').click()");
        await waitFor(host.panel, "document.querySelector('.panel-menu-content').getBoundingClientRect().bottom <= innerHeight", "停止菜单不得裁切");
        await capture(host.panel, "desktop-control-menu");
      }
      const selector = command === "stop" ? "#stop-work" : "#toggle-work";
      await run(host.panel, `document.querySelector('${selector}').click(); document.querySelector('${selector}').click()`);
      await waitFor(host.panel, "document.querySelector('#control-feedback').textContent.includes('已受理')", "控制回执未显示");
      assert.equal(controlChanges.length, { pause: 1, resume: 2, stop: 3 }[command], "重复点击不得重复请求");
      assert.equal(controlChanges.at(-1).loopId, "loop-workspace-a");
      assert.equal(controlChanges.at(-1).command, command);
      assert.equal(await run(host.panel, "document.querySelector('#stop-work').disabled"), true);
      if (command === "pause") assert.equal(await run(host.panel, "document.querySelector('#toggle-work').textContent"), "暂停");
      statusOverride = target; liveSequence++;
      streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: liveSequence })}\n\n`);
      await waitFor(host.panel, "document.querySelector('#control-feedback').textContent.includes('已生效')", "控制必须由 Live 确认生效");
      assert.equal(await run(host.panel, "document.querySelector('#toggle-work').textContent"), label);
      if (command === "pause") {
        assert.equal(await run(host.panel, "document.querySelector('#toggle-work').getBoundingClientRect().right <= document.querySelector('.panel-shell').getBoundingClientRect().right"), true);
        await capture(host.panel, "desktop-control-paused");
      }
    }
    assert.equal(await run(host.panel, "document.querySelector('#toggle-work').hidden && document.querySelector('#stop-work').disabled"), true);
    statusOverride = null; loopRevision++; liveSequence++;
    streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: liveSequence })}\n\n`);
    await waitFor(host.panel, "!document.querySelector('#toggle-work').disabled && !document.querySelector('#toggle-work').hidden", "新工作状态应恢复控制入口");
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
    // 真实 preload/异步写入 IPC/renderer 重建：本地草稿与材料不能依赖闭包或旧页面来源。
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
    await waitFor(host.panel, "!document.querySelector('#delivery').hidden", "已验收的常驻 Patrol 必须展示本次交付");
    assert.match(await run(host.panel, "document.querySelector('#status').textContent"), /本次工作已交付/);
    assert.match(await run(host.panel, "document.querySelector('#delivery').textContent"), /完成截图分析并生成报告.*验收通过 1 项 · 仍未解决 1 项/s);
    assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "A 的草稿");
    assert.equal(await run(host.panel, "document.querySelector('#toggle-work').disabled"), false);
    await run(host.panel, "document.querySelector('#delivery details').open=true; document.querySelector('#delivery').scrollIntoView({block:'start'})");
    await capture(host.panel, "desktop-accepted-delivery");
    assert.equal(await run(host.panel, "document.querySelector('#delivery').scrollWidth <= document.querySelector('#delivery').clientWidth + 1"), true);
    await run(host.panel, "document.querySelector('#delivery details').open=false; document.querySelector('[data-patrol-delivery-context]').click()");
    await run(workbench, "window.target=null;window.focusDesktop.patrol.onWorkbench(value=>window.target=value);window.focusDesktop.patrol.ready()");
    await waitFor(workbench, "window.target?.revisionId === 'revision-answer'", "交付入口必须传递验收时的固定结果版本");
    await waitFor(host.panel, "document.querySelector('#results').textContent.includes('截图中的机器人已经接住材料')", "已提交回答未显示");
    const resultText = await run(host.panel, "document.querySelector('#results').textContent");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-context-id]').dataset.contextId"), "context-answer");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-revision-id]').dataset.revisionId"), "revision-answer");
    assert.match(await run(host.panel, "document.querySelector('#results [data-context-id]').title"), /Context context-answer · Revision revision-answer/);
    assert.doesNotMatch(resultText, /隐藏推理|工具调用不可/);
    await capture(host.panel, "desktop-committed-answer");
    assert.match(await run(host.panel,"document.querySelector('#artifacts').textContent"),/报告.pdf/);

    assert.equal(await run(host.panel,"document.querySelector('.answer-reading').open"),false);
    assert.equal(await run(host.panel,"document.querySelector('.panel-shell').offsetWidth"),288);
    await run(host.panel,"document.querySelector('.answer-reading summary').click()");
    await waitFor(host.panel,"document.querySelector('.panel-shell').offsetWidth === 520", "展开阅读未拓宽面板");
    assert.equal(await run(host.panel,"getComputedStyle(document.querySelector('.answer-markdown')).fontSize"),"14px");
    assert.equal(await run(host.panel,"document.querySelector('.answer-markdown strong').textContent"),"已完成检查");
    assert.equal(await run(host.panel,"document.querySelector('.answer-markdown table') !== null"),true);
    assert.equal(await run(host.panel,"getComputedStyle(document.querySelector('.answer-markdown pre')).maxHeight"),"none");
    assert.equal(await run(host.panel,"Boolean(window.answerInjected)"),false);
    await run(host.panel,"window.copiedAnswers=[]; Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.copiedAnswers.push(text)}}); document.querySelector('.answer-content [data-action=copy-answer]').click()");
    await waitFor(host.panel,"window.copiedAnswers.length === 1", "全文复制未完成");
    await run(host.panel,"document.querySelector('.message-code-block [data-action=copy-code]').click()");
    await waitFor(host.panel,"window.copiedAnswers.length === 2", "代码复制未完成");
    assert.deepEqual(await run(host.panel,"window.copiedAnswers"),[answerText(answerRevision),'const value = "<safe>";\n']);
    await capture(host.panel,"desktop-answer-reading");
    await run(host.panel,"document.querySelector('.message-code-block').scrollIntoView({block:'center'}); document.querySelector('.answer-markdown a[href=\"https://example.com/result\"]').click()");
    await waitFor(host.panel,"document.querySelector('#error').hidden", "回答外链打开失败");
    assert.deepEqual(externalLinks,["https://example.com/result"]);
    await run(host.panel, "[...document.querySelectorAll('.answer-markdown a')].find(a=>a.textContent==='查看报告').click()");
    await waitFor(host.panel,"document.querySelector('#version-body').textContent.includes('正确执行位置的报告')", "正文中文空格链接未进入共享预览器");
    assert.match(await run(host.panel,"document.querySelector('#version-body').textContent"),/当前磁盘文件/);
    await run(host.panel,"document.querySelector('#version-body [data-source-toggle]').click()");
    assert.equal(await run(host.panel,"Boolean(document.querySelector('#version-body .file-source'))"),true);
    await run(host.panel,"document.querySelector('#version-body [data-file-operation=open]').click()");
    await waitFor(host.panel,"document.querySelector('#version-body [data-file-status]').textContent === '已完成'", "正文文件未接通原生操作");
    assert.equal(sharedActions.at(-1).target.link.slot_id,"slot-a");
    assert.equal(sharedActions.at(-1).target.link.run_id,"run-answer");
    await run(host.panel,"document.querySelector('#close-version-detail').click()");
    assert.ok(host.panel.webContents.getURL().includes("desktop-patrol.html?surface=panel"));
    await capture(host.panel,"desktop-answer-code");
    await run(host.panel,"document.querySelector('.answer-reading summary').click()");
    await waitFor(host.panel,"document.querySelector('.panel-shell').offsetWidth === 288", "收起阅读未恢复小面板");
    assert.match(await run(host.panel,"document.querySelector('#artifacts').textContent"),/semantic_indexer.py/);
    await run(host.panel,"[...document.querySelectorAll('.artifact-result')].find(row=>row.textContent.includes('semantic_indexer.py')).querySelectorAll('button')[1].click()");
    await waitFor(host.panel,"document.querySelector('#file-refs').textContent.includes('semantic_indexer.py')", "产物无法直接引用");
    await run(host.panel,"document.querySelector('.file-ref>button:last-child').click(); [...document.querySelectorAll('.artifact-result')].find(row=>row.textContent.includes('semantic_indexer.py')).querySelector('button').click()");
    await waitFor(host.panel,"!document.querySelector('#version-detail').hidden && document.querySelector('#version-body').textContent.includes('ProtocolSafeRevisionSegmenter')", "产物未打开固定版本");
    await run(host.panel,"document.querySelector('#close-version-detail').click()");

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
    await run(workbench, "window.target=null");
    await run(host.panel, "document.querySelector('#results [data-context-id]').click()");
    for (let i = 0; i < 120 && (!workbench || !workbench.webContents.getURL().endsWith("/desktop/")); i++) await delay(40);
    assert.ok(workbench);
    await waitFor(workbench, "Boolean(window.target)", "工作台未收到来源定位");
    assert.deepEqual(await run(workbench, "window.target"), { workspaceId: "workspace-a", taskId: "context-answer", revisionId: answerRevision });
    await host.openPanel();
    await run(host.panel,"document.querySelector('#open-files').click()");
    await waitFor(host.panel,"document.querySelectorAll('.artifact-row').length === 3","产物抽屉没有按文件合并历史");
    await run(host.panel,"document.querySelector('[data-files-search]').value='报告'; document.querySelector('[data-files-search]').dispatchEvent(new Event('input',{bubbles:true}))");
    assert.equal(await run(host.panel,"document.querySelectorAll('.artifact-row').length"),1);
    await waitFor(host.panel,"innerWidth >= 580","文件抽屉未展开窗口宽度");
    await waitFor(host.panel,"(() => { const drawer=document.querySelector('.artifact-drawer').getBoundingClientRect(), button=document.querySelector('[data-files-native=saveAs]').getBoundingClientRect(); return button.top>=drawer.top && button.bottom<=drawer.bottom; })()","报告的另存按钮应直接可见");
    await capture(host.panel,"desktop-artifact-drawer");
    assert.equal(await run(host.panel,"document.querySelector('.artifact-drawer').scrollWidth <= document.querySelector('.artifact-drawer').clientWidth"),true,"文件抽屉不能横向溢出");
    await run(host.panel,"document.querySelector('[data-files-native=open]').click()");
    await waitFor(host.panel,"document.querySelector('[data-files-status]').textContent === '已打开'","文件打开未返回结果");
    await run(host.panel,"document.querySelector('[data-files-native=reveal]').click()");
    await run(host.panel,"document.querySelector('[data-files-native=saveAs]').click()");
    await waitFor(host.panel,"document.querySelector('[data-files-status]').textContent === '已另存文件'","另存文件未返回结果");
    assert.deepEqual(fs.readFileSync(savedReport),reportBytes);
    assert.deepEqual(nativeExits,[["open",reportPath],["reveal",reportPath]]);
    await run(host.panel,"document.querySelector('.artifact-actions [data-files-view]').click()");
    await waitFor(host.panel,"document.querySelector('[data-files-preview] [data-page-label]')?.textContent === '1/2'","文件抽屉 PDF 未挂载共享分页预览");
    await run(host.panel,"document.querySelector('[data-next-page]').click()");
    await waitFor(host.panel,"document.querySelector('[data-page-label]').textContent === '2/2'","抽屉 PDF 无法翻页");
    await run(host.panel,"document.querySelector('[data-files-preview] [data-file-operation=save]').click()");
    await waitFor(host.panel,"document.querySelector('[data-files-preview] [data-file-status]').textContent === '已完成'","抽屉固定 PDF 未接通另存");
    assert.deepEqual(sharedActions.at(-1).target,{loop_id:"loop-workspace-a",fact_id:"report-file",revision:1});
    await run(host.panel,"document.querySelector('.artifact-history [data-files-view]').click()");
    await waitFor(host.panel,"document.querySelector('[data-files-preview]').textContent.includes('报告初稿')","报告历史快照不可查看");
    assert.equal(await run(host.panel,"Boolean(document.querySelector('.artifact-history [data-files-native]'))"),false);
    await run(host.panel,"document.querySelector('[data-files-close]').click()");
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
    await waitFor(host.panel,"document.querySelector('#history-status').textContent.includes('暂时读取失败') && document.querySelector('#results').textContent.includes('截图中的机器人')","历史读取失败时应继续展示 Live 结果");
    assert.equal(await run(host.panel,"document.querySelector('#error').hidden"),true,"历史失败仅留在记录面板");
    for (let i=0; i<100 && !streams.has('loop-workspace-a'); i++) await delay(20);
    const historyRetryStream = streams.get('loop-workspace-a'), lookupsBeforeRetry = workspaceReads.length;
    assert.ok(historyRetryStream,"历史失败时仍建立 Live 连接");
    await run(host.panel,"document.querySelector('#retry-history').click()");
    await waitFor(host.panel,"document.querySelector('#history-status').hidden && document.querySelector('#history-items').textContent.includes('较早的输入')","历史重试应恢复记录");
    assert.equal(workspaceReads.length,lookupsBeforeRetry,"历史重试不能重新查询 Loop");
    assert.equal(streams.get('loop-workspace-a'),historyRetryStream,"历史重试不能重建 Live 连接");
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
    for (const terminal of ["stopped", "failed"]) {
      const loopId = currentLoops.get("workspace-a") || "loop-workspace-a";
      for (let i = 0; i < 120 && !streams.has(loopId); i++) await delay(20);
      assert.ok(streams.has(loopId), "重新启用验收前工作状态应已连接");
      statusOverride = terminal; liveSequence++; loopRevision++;
      streams.get(loopId).write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: liveSequence })}\n\n`);
      await waitFor(host.panel, "!document.querySelector('#restart-work').hidden && !document.querySelector('#restart-work').disabled", "终态应原位显示重新启用");
      const before = requests.length, restartCount = restartRequests.length;
      await run(host.panel, "document.querySelector('#instruction').value='继续，换个方案'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click()");
      await waitFor(host.panel, "document.querySelector('#recovery-message').textContent === '这条输入已保存，重新启用后处理。' && document.querySelector('#receipt').textContent === '已保存'", "已保存和重新启用必须区分");
      assert.equal(requests.length, before + 1);
      assert.equal(restartRequests.length, restartCount, "发送不得自动重新启用");
      await capture(host.panel, `desktop-saved-${terminal}`);
      await run(host.panel, "document.querySelector('#instruction').value='保留后续草稿'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
      if (terminal === "stopped") {
        restartError = true;
        await run(host.panel, "document.querySelector('#restart-work').click()");
        await waitFor(host.panel, "!document.querySelector('#error').hidden && !document.querySelector('#restart-work').disabled", "重新启用失败应保留入口");
        assert.match(await run(host.panel, "document.querySelector('#error').textContent"), /尚未安全结算/);
        restartError = false;
      }
      const attempts = restartRequests.length;
      holdRestartSnapshot = true; releaseRestartSnapshot = null;
      await run(host.panel, "document.querySelector('#restart-work').click(); document.querySelector('#restart-work').click()");
      await waitFor(host.panel, "document.querySelector('#recovery-message').textContent.includes('正在确认工作状态')", "HTTP 回执不能声称已经恢复");
      assert.equal(restartRequests.length, attempts + 1);
      assert.equal(await run(host.panel, "document.querySelector('#restart-work').disabled"), true);
      for (let i = 0; i < 120 && !releaseRestartSnapshot; i++) await delay(20);
      assert.ok(releaseRestartSnapshot);
      holdRestartSnapshot = false; releaseRestartSnapshot();
      await waitFor(host.panel, "document.querySelector('#recovery-message').textContent === '工作已重新启用。' && document.querySelector('#restart-work').hidden", "Live 确认后才显示重新启用成功");
      assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "保留后续草稿");
      assert.equal(requests.length, before + 1, "重新启用不得重复提交输入");
      await capture(host.panel, `desktop-reactivated-${terminal}`);
    }
    assert.deepEqual(errors, []);
    console.log("PATROL_UI_PASS preview-before-binding, pathless-image, screenshot-paste, remove, explicit-send, IME guard, CtrlEnter, workspace isolation, pause-resume-stop, control-acceptance-and-Live, hide/restore, accepted-is-not-complete, committed-answer-source, independent-progress, hidden-refresh-budget, revision-cache, reload-recovery, new-port-recovery, lost-receipt-reconciliation");
  }
  app.whenReady().then(execute).then(() => cleanup(0)).catch(error => { console.error(error.stack || error); console.error(errors.join("\n")); cleanup(1); });
  function cleanup(code) {
    host?.dispose();
    if (workbench && !workbench.isDestroyed()) workbench.destroy();
    server?.closeAllConnections(); server?.close();
    app.exit(code);
  }
}
