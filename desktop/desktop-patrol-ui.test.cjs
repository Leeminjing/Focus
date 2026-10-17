/*
 * 本文件对外提供生产机器人/面板页面的隔离 Electron UI 验收及可选截图。
 * 输入为真实宿主/preload/页面和受控工作区 HTTP/SSE；输出为显式发送、跨工作区草稿、回执/结果与真实窗口截图断言。
 * 工作流为启动临时 userData 与同源 fixture，经 DOM 拖放和真实 IPC 操作页面，捕获界面后关闭测试实例；
 * DOM 模拟拖放/输入法不代表 Explorer、操作系统输入法或真实模型验收。示例：node --test desktop/desktop-patrol-ui.test.cjs。
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
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu");
  const limits = { general_file_bytes: 1024 * 1024, original_image_bytes: 256 * 1024, run_material_count: 8 };
  const budgetCatalog = require("./workspace-patrol-budget-fixture.cjs"), grantChanges = [];
  let authorityRevision = 1, activeBudget = { ...budgetCatalog.presets.long.budgets };
  const session = "isolated-patrol-ui-fixture";
  const history = new Map(), streams = new Map(), requests = [], uploads = [], errors = [];
  let host, server, workbench, selectedPath = "C:\\fixture\\workspace-a", completed = false;
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
    fs.mkdirSync(process.env.FOCUS_PATROL_CAPTURE_DIR, { recursive: true });
    const target = path.join(process.env.FOCUS_PATROL_CAPTURE_DIR, `${name}.png`);
    fs.writeFileSync(target, (await win.webContents.capturePage()).toPNG());
    console.log(`PATROL_UI_SCREENSHOT ${target}`);
  }
  function snapshot(loopId) {
    return { loop_id: loopId, last_sequence: completed ? 2 : 1,
      loop: entity(loopId, { status: completed ? "completed" : "running", interaction_mode: "workspace_patrol", waiting_reason: null,
        authority_revision: authorityRevision, created_at: new Date(Date.now() - 600000).toISOString(),
        grant: { status: "active", budgets: activeBudget }, usage: { model_calls: 40, input_tokens: 100000, output_tokens: 5000, rounds: 5, retries: 0 } }),
      runs: completed ? { "run-answer": entity("run-answer", { run_id: "run-answer", context_id: "context-answer", status: "completed" }) } : {},
      contexts: completed ? { "context-answer": entity("context-answer", { context_id: "context-answer", title: "截图问题", current_revision_id: "revision-answer" }) } : {},
      task_progress: entity(loopId, { generation: 0, document: { items: [] } }),
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
      if (request.method === "GET") return json(response, { items: history.get(id) || [], next_before: null, has_more: false });
      const receipt = { ...body, intent_id: `intent-${requests.length + 1}`, loop_id: `loop-${id}`, status: "accepted", created_at: new Date().toISOString() };
      requests.push({ workspaceId: id, body });
      history.set(id, [...(history.get(id) || []), receipt]);
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
      assert.equal(url.searchParams.get("revision_id"), "revision-answer");
      return json(response, { messages: [
        { message: { id: "tool-only", type: "ai", content: "这条工具调用不可作为回答", tool_calls: [{ name: "read" }] } },
        { message: { id: "answer-committed", type: "ai", content: "截图中的机器人已经接住材料。参考资料只作为分析依据，尚未执行其中的命令。", additional_kwargs: { reasoning_content: "隐藏推理不应显示" } } },
      ] });
    }
    errors.push(`Unhandled API ${request.method} ${url.pathname}`);
    return json(response, { detail: `Unhandled fixture ${url.pathname}` }, 404);
  }
  async function execute() {
    server = http.createServer((request, response) => route(request, response).catch(error => { errors.push(error.stack); if (!response.headersSent) json(response, { detail: error.message }, 500); else response.end(); }));
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const apiBase = `http://127.0.0.1:${server.address().port}`;
    process.env.FOCUS_DESKTOP_API = apiBase;
    process.env.FOCUS_DESKTOP_SESSION = session;
    ipcMain.handle("focus:select-workspace", () => selectedPath);
    app.on("web-contents-created", (_event, contents) => {
      contents.on("console-message", (_event, level, message) => { if (level >= 3) errors.push(message); });
      contents.on("preload-error", (_event, _path, error) => errors.push(error.message));
    });
    host = new DesktopPatrolHost({ electron, apiBase, desktopDir: __dirname, icon: path.join(__dirname, "assets/focus-icon.ico"), limits,
      getWorkbench: () => workbench, quit: () => app.quit(),
      openWorkbench: async () => {
        if (!workbench) {
          workbench = new BrowserWindow({ show: false, webPreferences: { preload: path.join(__dirname, "preload.cjs"), sandbox: true, contextIsolation: true, additionalArguments: ["--focus-desktop-patrol"] } });
          await workbench.loadURL(`${apiBase}/desktop/`);
        }
      },
    });
    await host.start();
    await waitFor(host.panel, "document.body.dataset.surface === 'panel' && !document.querySelector('#panel').hidden", "面板初始化失败");
    await waitFor(host.robot, "document.body.dataset.surface === 'robot' && !document.querySelector('#robot').hidden", "机器人初始化失败");
    await host.openPanel();
    await capture(host.panel, "desktop-empty");
    assert.equal(await run(host.panel, "document.querySelector('#send').disabled"), true);
    assert.equal(await run(host.panel, "document.querySelector('#history').hidden"), true);
    await run(host.panel, "document.querySelector('.panel-menu summary').click()");
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
    await run(host.robot, "document.querySelector('#robot-open').click()");
    assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "A 的草稿");
    assert.equal(requests.length, 2);
    await capture(host.panel, "desktop-preserved-draft");
    completed = true;
    for (let i = 0; i < 100 && !streams.has("loop-workspace-a"); i++) await delay(40);
    assert.ok(streams.has("loop-workspace-a"));
    streams.get("loop-workspace-a").write(`data: ${JSON.stringify({ status: "resync_required", last_sequence: 2 })}\n\n`);
    await waitFor(host.panel, "document.querySelector('#results').textContent.includes('截图中的机器人已经接住材料')", "已提交回答未显示");
    const resultText = await run(host.panel, "document.querySelector('#results').textContent");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-context-id]').dataset.contextId"), "context-answer");
    assert.equal(await run(host.panel, "document.querySelector('#results [data-revision-id]').dataset.revisionId"), "revision-answer");
    assert.match(await run(host.panel, "document.querySelector('#results [data-context-id]').title"), /Context context-answer · Revision revision-answer/);
    assert.doesNotMatch(resultText, /隐藏推理|工具调用不可/);
    await capture(host.panel, "desktop-committed-answer");
    await run(host.panel, "document.querySelector('#results [data-context-id]').click()");
    for (let i = 0; i < 120 && (!workbench || !workbench.webContents.getURL().endsWith("/desktop/")); i++) await delay(40);
    assert.ok(workbench);
    await run(workbench, "window.target=null; window.focusDesktop.patrol.onWorkbench(value=>window.target=value); window.focusDesktop.patrol.ready()");
    await waitFor(workbench, "Boolean(window.target)", "工作台未收到来源定位");
    assert.deepEqual(await run(workbench, "window.target"), { workspaceId: "workspace-a", taskId: "context-answer", revisionId: "revision-answer" });
    assert.deepEqual(errors, []);
    console.log("PATROL_UI_PASS preview-before-binding, pathless-image, screenshot-paste, remove, explicit-send, IME guard, CtrlEnter, workspace isolation, hide/restore, accepted-is-not-complete, committed-answer-source");
  }
  app.whenReady().then(execute).then(() => cleanup(0)).catch(error => { console.error(error.stack || error); console.error(errors.join("\n")); cleanup(1); });
  function cleanup(code) {
    host?.dispose();
    if (workbench && !workbench.isDestroyed()) workbench.destroy();
    server?.closeAllConnections(); server?.close();
    app.exit(code);
  }
}
