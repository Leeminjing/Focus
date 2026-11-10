/* 使用生产工作台、preload、机器人面板与真实 IPC，验证干净用户从模型设置走到第一次发送。
 * 同时验证常规模型卡与字段错误的可访问性。HTTP 模型验证与目录选择为隔离 fixture。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

if (!process.versions.electron) {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  test("fresh Windows user reaches the first send through production settings and desktop panel", { timeout: 60000 }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-first-run-"));
    const env = { ...process.env, FOCUS_FIRST_RUN_USER_DATA: directory };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], { windowsHide: true, env, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", chunk => { output += chunk; });
    child.stderr.on("data", chunk => { output += chunk; });
    const timer = setTimeout(() => child.kill(), 55000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, output);
      assert.match(output, /FIRST_RUN_UI_PASS/);
    } finally {
      clearTimeout(timer);
      assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  });
} else {
  const electron = require("electron"), { app, BrowserWindow, ipcMain } = electron;
  const http = require("node:http");
  const { setTimeout: delay } = require("node:timers/promises");
  const { DesktopPatrolHost } = require("../../src/main/desktop-patrol-host.cjs");
  const { WorkspacePatrolRecoveryHost } = require("../../src/main/workspace-patrol-recovery-host.cjs");
  const directory = process.env.FOCUS_FIRST_RUN_USER_DATA;
  app.setPath("userData", directory);
  app.disableHardwareAcceleration();
  app.on("window-all-closed", () => {});
  const session = "first-run-fixture", errors = [], requests = [], modelSaves = [];
  const limits = { general_file_bytes: 1024 * 1024, run_material_count: 8 };
  let host, server, workbench, apiBase, picked = 0, allowVerification = false, rejectModelEdit = false;
  let snapshot = { setup_required: true, setup_issues: { api_key: "请输入真实 API Key" },
    models: [{ name: "first", model: "first", use: "langchain_openai:ChatOpenAI", api_key: "$FIRST_API_KEY", api_key_set: false,
      default: true, curation_default: true, base_url: "https://fixture.invalid/v1", context_window: 8192,
      curation_output_method: "prompt_json", curation_max_output_tokens: 8192 }],
    editable_fields: ["name", "model", "use", "api_key", "base_url", "context_window", "curation_output_method", "curation_max_output_tokens", "default", "curation_default"],
    adapters: [{ use: "langchain_openai:ChatOpenAI", label: "OpenAI 兼容" }], shipped_models: [], curation_output_methods: ["prompt_json"] };
  const json = (res, value) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(value)); };
  const workspace = { workspace_id: "project", display_name: "第一个项目", path: "C:\\fixture\\project" };
  async function route(req, res) {
    const url = new URL(req.url, apiBase);
    if (!url.pathname.startsWith("/desktop/api/")) {
      const relative = url.pathname === "/desktop/" ? "index.html" : decodeURIComponent(url.pathname.replace(/^\/desktop\//, ""));
      const file = path.resolve(__dirname, "../../src/renderer", relative);
      if (!file.startsWith(path.resolve(__dirname, "../../src/renderer") + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
      const type = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" }[path.extname(file)];
      res.writeHead(200, { "Content-Type": type || "application/octet-stream" }); res.end(fs.readFileSync(file)); return;
    }
    assert.equal(req.headers["x-focus-session"], session);
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
    if (url.pathname.endsWith("/settings/models")) {
      if (req.method === "PUT") {
        if (rejectModelEdit) {
          res.writeHead(422, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ detail: { code: "model_settings_invalid", errors: {
            "models[1].base_url": "必须是 http 或 https 的绝对地址", default: "必须恰好指定一个默认模型",
          } } })); return;
        }
        modelSaves.push(body);
        snapshot = { ...snapshot, setup_required: false, setup_issues: {},
          models: snapshot.models.map(model => ({ ...model, api_key_set: true })),
          verification: { ok: allowVerification, reason: allowVerification ? "全部验证通过" : "策展验证失败，请修改输出方式",
            checks: ["chat", "curation", "semantic_index"].map(capability => ({ capability,
              ok: capability !== "curation" || allowVerification,
              status: capability !== "curation" || allowVerification ? "passed" : "failed" })) } };
      }
      return json(res, snapshot);
    }
    if (url.pathname.endsWith("/bootstrap")) return json(res, { tasks: [], equipment: { models: snapshot.models, tools: [], tools_status: "deferred" } });
    if (url.pathname.endsWith("/sessions/archived") || url.pathname === "/desktop/api/workspaces") return json(res, req.method === "POST" ? workspace : []);
    if (url.pathname.endsWith("/sandbox/status")) return json(res, { available: true, platform: "win32" });
    if (url.pathname.endsWith("/shell-recovery")) return json(res, { items: [] });
    if (url.pathname.endsWith("/materials/limits")) return json(res, limits);
    if (url.pathname.endsWith("/budget-presets")) return json(res, require("../fixtures/workspace-patrol-budget-fixture.cjs"));
    if (url.pathname.endsWith("/workspaces/project")) return json(res, workspace);
    if (url.pathname.endsWith("/workspace/project")) return json(res, null);
    if (url.pathname.endsWith("/workspace/project/inputs")) {
      if (req.method === "POST") { requests.push(body); return json(res, { ...body, status: "accepted", loop_id: "loop-first" }); }
      return json(res, { items: [], next_before: null });
    }
    if (url.pathname.endsWith("/loop-first/live")) return json(res, { loop_id: "loop-first", last_sequence: 0,
      loop: { entity_id: "loop-first", revision: 1, updated_sequence: 0, state: { status: "running" } } });
    if (url.pathname.endsWith("/live/stream")) { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(": ready\n\n"); return; }
    if (url.pathname.endsWith("/live/history")) return json(res, { items: [] });
    throw new Error(`Unhandled fixture ${req.method} ${url.pathname}`);
  }
  const run = (win, code) => win.webContents.executeJavaScript(code);
  async function capture(win, name) {
    const output = process.env.FOCUS_FIRST_RUN_CAPTURE_DIR;
    if (!output) return;
    await run(win, "document.fonts.ready");
    await delay(450);
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, `${name}.png`), (await win.webContents.capturePage()).toPNG());
  }
  async function waitFor(win, expression) {
    for (let i = 0; i < 160; i++) { if (await run(win, expression)) return; await delay(30); }
    throw new Error(`等待失败 ${expression}\n${await run(win, "document.body.innerText")}\n${errors.join("\n")}`);
  }
  async function execute() {
    server = http.createServer((req, res) => route(req, res).catch(error => { errors.push(error.stack); res.writeHead(500); res.end(); }));
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    apiBase = `http://127.0.0.1:${server.address().port}`;
    process.env.FOCUS_DESKTOP_API = apiBase; process.env.FOCUS_DESKTOP_SESSION = session;
    new WorkspacePatrolRecoveryHost({ ipcMain, directory, byteLimit: limits.general_file_bytes, getWorkbench: () => workbench, origin: apiBase });
    require("../../src/main/desktop-preferences.cjs").registerDesktopPreferences(ipcMain, {
      directory: () => directory,
      authorize: event => assert.equal(new URL(event.senderFrame.url).origin, apiBase),
    });
    ipcMain.handle("focus:gateway-status", () => ({ status: "ready", error: null, revision: 1 }));
    ipcMain.handle("focus:notifications-ready", () => true);
    ipcMain.handle("focus:notifications-settings", () => ({ enabled: true }));
    ipcMain.handle("focus:get-zoom-level", () => 0);
    ipcMain.handle("focus:set-zoom-level", () => 0);
    ipcMain.handle("focus:select-workspace", () => ++picked === 1 ? "" : workspace.path);
    app.on("web-contents-created", (_event, contents) => {
      contents.on("preload-error", (_event, _path, error) => errors.push(error.message));
      contents.on("console-message", (_event, level, message) => { if (level >= 3) errors.push(message); });
    });
    host = new DesktopPatrolHost({ electron, apiBase, desktopDir: path.resolve(__dirname, "../.."), icon: path.join(__dirname, "../../src/renderer/assets/focus-icon.ico"), limits,
      getWorkbench: () => workbench, quit: () => app.quit(),
      getModelSettings: async () => (await fetch(`${apiBase}/desktop/api/settings/models`, { headers: { "X-Focus-Session": session } })).json(),
      openWorkbench: async () => {
        if (!workbench) {
          workbench = new BrowserWindow({ show: false, width: 1100, height: 800, webPreferences: { preload: path.join(__dirname, "../../src/preload/preload.cjs"), sandbox: true, contextIsolation: true, additionalArguments: ["--focus-desktop-patrol"] } });
          await workbench.loadURL(`${apiBase}/desktop/`);
        }
        workbench.show();
      },
    });
    await host.start();
    await waitFor(workbench, "document.querySelector('#settingsDialog').open && !!document.querySelector('[data-model-credential]')");
    await capture(workbench, "first-run-model-settings");
    assert.equal(host.panel.isVisible(), false);
    assert.equal(await run(workbench, `document.querySelector('[data-action="model-save"]').textContent`), "保存并验证");
    assert.equal(await run(workbench, `document.querySelector('[data-i18n="settings.models_help"]').hidden`), true);
    assert.equal(await run(workbench, `document.querySelector('#modelSetupSteps [aria-current="step"]').textContent.includes('1. 模型配置')`), true);
    await run(workbench, `(() => {const key=document.querySelector('[data-model-credential]');key.value='fixture-key';key.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-action="model-save"]').click();})()`);
    await waitFor(workbench, "document.querySelector('#modelSettingsHost').textContent.includes('策展验证失败')");
    assert.equal(host.panel.isVisible(), false);
    assert.equal(modelSaves[0].credentials.FIRST_API_KEY, "fixture-key");
    allowVerification = true;
    await run(workbench, "document.querySelector('[data-action=\"model-save\"]').click()");
    await waitFor(workbench, "!!document.querySelector('[data-action=\"model-start\"]')");
    await capture(workbench, "first-run-model-verified");
    assert.equal(await run(workbench, `document.querySelectorAll('[data-check-status="passed"]').length`), 3);
    await run(workbench, `document.querySelector('#modelVerificationHost').scrollIntoView({block:'end'})`);
    await capture(workbench, "first-run-verification-results");
    await run(workbench, `document.querySelector('#modelSetupSteps').scrollIntoView({block:'start'})`);
    assert.equal(await run(workbench, `document.querySelector('[data-action="model-start"]').textContent`), "选择项目文件夹");
    assert.equal(picked, 0, "验证通过后仍需显式选择项目");
    await run(workbench, `(() => {
      const key = document.querySelector('[data-model-credential]');
      key.focus(); key.value = 'updated-fixture-key'; key.setSelectionRange(4, 4);
      key.dispatchEvent(new Event('input', { bubbles: true }));
      if (document.activeElement !== key || key.selectionStart !== 4) throw new Error('编辑配置打断了输入焦点');
    })()`);
    assert.equal(await run(workbench, `!!document.querySelector('[data-action="model-start"]')`), false);
    assert.equal(await run(workbench, `!!document.querySelector('#modelVerificationHost .is-ok')`), false);
    assert.equal(await run(workbench, `document.body.innerText.includes('设置已保存。全部验证通过')`), false);
    assert.equal(await run(workbench, `document.querySelector('#modelSetupSteps [aria-current="step"]').textContent.includes('1. 模型配置')`), true);
    await capture(workbench, "first-run-model-edited");
    await run(workbench, `document.querySelector('[data-action="model-save"]').click()`);
    await waitFor(workbench, "!!document.querySelector('[data-action=\"model-start\"]')");
    assert.equal(host.panel.isVisible(), false);
    assert.equal(host._setupStep, "models");
    assert.equal(await run(workbench, "document.querySelector('#settingsDialog').open"), true);
    await run(workbench, "document.querySelector('[data-action=\"model-start\"]').click()");
    await waitFor(host.panel, "!document.querySelector('#first-goal').hidden && document.querySelector('#first-goal-folder').hidden === false");
    assert.equal(workbench.isVisible(), false);
    assert.equal(picked, 1);
    await capture(host.panel, "first-run-folder-selection");
    assert.equal(requests.length, 0);
    await run(host.panel, "document.querySelector('#first-goal-folder').click()");
    await waitFor(host.panel, "document.querySelector('#binding-name').textContent === '第一个项目'");
    await capture(host.panel, "first-run-goal");
    await run(host.panel, `(() => {const input=document.querySelector('#instruction');input.value='整理项目待办';input.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#send').click();})()`);
    await waitFor(host.panel, "document.querySelector('#receipt').textContent === '已受理'");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].content, "整理项目待办");
    assert.equal(modelSaves.length, 3);
    assert.equal(modelSaves[2].credentials.FIRST_API_KEY, "updated-fixture-key");
    // 常规设置：主层只显示用途和真实状态；字段错误不重复进入全局区域。
    snapshot = { ...snapshot, default_model_override: "first", models: [
      { ...snapshot.models[0], display_name: "主力模型", source: "shipped", provider: "openai", protocol: "chat_completions" },
      { ...snapshot.models[0], name: "optional", display_name: "备用模型", default: false, curation_default: false },
      { ...snapshot.models[0], name: "template", display_name: "待配置模型", default: false, curation_default: false, api_key_set: false },
    ] };
    await run(workbench, `modelSettings.snapshot = null; modelSettings.draft = null;
      modelSettings.setupMode = false; modelSettings.onboarding = false;
      document.querySelector('[data-action="open-settings"]').click();`);
    workbench.show();
    await waitFor(workbench, "document.querySelectorAll('.settings-model-row').length === 3");
    await run(workbench, `document.querySelector('[data-settings-tab="models"]').click()`);
    assert.equal(await run(workbench, `document.querySelector('.settings-model-row').textContent.includes('已通过验证')`), true);
    assert.equal(await run(workbench, `document.querySelectorAll('.settings-model-row')[1].textContent.includes('未验证')`), true);
    assert.equal(await run(workbench, `/fixture.invalid|8192|chat_completions/.test(document.querySelector('.settings-model-list').textContent)`), false);
    await capture(workbench, "model-cards");
    await run(workbench, `document.querySelector('[data-action="model-edit"][data-model-index="1"]').click()`);
    await waitFor(workbench, `!!document.querySelector('#model-1-base_url')`);
    await run(workbench, `const endpoint = document.querySelector('[data-model-field="base_url"]');
      endpoint.value = 'invalid'; endpoint.dispatchEvent(new Event('input',{bubbles:true}));`);
    rejectModelEdit = true;
    await run(workbench, `document.querySelector('[data-action="model-save"]').click()`);
    await waitFor(workbench, `document.activeElement.id === 'model-1-base_url'`);
    assert.equal(await run(workbench, `document.querySelector('#model-1-base_url').getAttribute('aria-invalid')`), "true");
    assert.equal(await run(workbench, `document.querySelector('#model-1-base_url').labels[0].textContent`), "端点");
    assert.equal(await run(workbench, `document.getElementById(document.querySelector('#model-1-base_url').getAttribute('aria-describedby')).textContent`), "必须是 http 或 https 的绝对地址");
    assert.equal(await run(workbench, `document.querySelector('.settings-model-errors').textContent`), "必须恰好指定一个默认模型");
    assert.equal(await run(workbench, `document.querySelector('#globalStatus').textContent.includes('正在验证')`), false);
    assert.equal(await run(workbench, `document.querySelectorAll('[data-model-credential]').length`), 1);
    assert.equal(await run(workbench, `document.querySelector('[data-model-credential]').value`), "");
    await capture(workbench, "model-field-error");
    await run(workbench, `const fixed = document.querySelector('#model-1-base_url');
      fixed.value='https://fixture.invalid/v1'; fixed.dispatchEvent(new Event('input',{bubbles:true}));`);
    assert.equal(await run(workbench, `document.querySelector('#model-1-base_url').hasAttribute('aria-invalid')`), false);
    assert.equal(await run(workbench, `!!document.querySelector('#model-1-base_url-error')`), false);
    assert.deepEqual(errors, []);
    await host.flushRecovery();
    console.log("FIRST_RUN_UI_PASS");
  }
  app.whenReady().then(execute).then(() => finish(0)).catch(error => { console.error(error.stack); console.error(errors.join("\n")); finish(1); });
  function finish(code) { host?.dispose(); workbench?.destroy(); server?.closeAllConnections(); server?.close(); app.exit(code); }
}
