/*
 * 本文件对 F20 全部主要工作台执行真实 Electron 稳定性矩阵。输入为确定性任务/Context/插件数据、
 * 11 类界面状态与 5 组尺寸缩放，输出为未捕获错误、DOM/ARIA、交互嵌套和页面溢出断言。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-f20-view-audit-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const cases = [
  { width: 1440, height: 1024, zoom: 1 },
  { width: 1200, height: 800, zoom: 1 },
  { width: 1200, height: 800, zoom: 1.25 },
  { width: 900, height: 680, zoom: 1 },
  { width: 900, height: 680, zoom: 1.5 },
];
const targets = ["focus", "map", "context", "draft", "agents", "commitment", "compression", "materials", "plugins", "file", "dialog", "empty", "error"];
const qaDir = process.env.FOCUS_QA_DIR ? path.resolve(process.env.FOCUS_QA_DIR) : "";
const captureAllViewports = process.env.FOCUS_QA_ALL_VIEWPORTS === "1";
if (qaDir) fs.mkdirSync(qaDir, { recursive: true });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function run() {
  const rendererErrors = [];
  const win = new BrowserWindow({
    show: false,
    width: 1440,
    height: 1024,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  win.webContents.on("console-message", (_event, level, message) => {
    if (level >= 3) rendererErrors.push(message);
  });
  win.webContents.on("render-process-gone", (_event, details) => rendererErrors.push(`renderer gone: ${details.reason}`));
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 150; count += 1) {
      if (document.querySelector('.focus-view')) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('Focus 启动超时'));
  })`);
  await win.webContents.insertCSS(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "style.css"), "utf8"));
  await win.webContents.executeJavaScript(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "viewer.js"), "utf8"));
  await win.webContents.executeJavaScript(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "entry.js"), "utf8"));
  await win.webContents.executeJavaScript(`(() => {
    window.__f20AuditErrors = [];
    window.addEventListener('error', event => window.__f20AuditErrors.push('error:' + event.message));
    window.addEventListener('unhandledrejection', event => window.__f20AuditErrors.push('rejection:' + String(event.reason)));
    window.__f20PrepareAuditTarget = async target => {
      document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
      state.filesPanel = null;
      state.inspector.open = false;
      state.inspector.returnFocus = null;
      state.contextDraft = null;
      state.commitment.review = null;
      state.commitment.recovery = null;
      state.commitment.stage = 0;
      if (target !== 'empty' && window.__auditSavedTasks) {
        state.tasks = window.__auditSavedTasks;
        window.__auditSavedTasks = null;
      }
      if (target === 'empty') {
        window.__auditSavedTasks = state.tasks;
        state.tasks = [];
        state.view = 'focus';
        render();
        return;
      }
      const task = state.tasks.find(item => item.task_id === 'child') || state.tasks[0];
      state.activeTaskId = task.task_id;
      if (target === 'focus') {
        state.view = 'focus'; render();
      } else if (target === 'map') {
        state.view = 'map'; render();
      } else if (target === 'context') {
        await reopenContextDecision('blocked');
      } else if (target === 'draft') {
        state.equipment = { models: [{ name: 'local-model', display_name: 'Local Model', context_window: 32000 }], tools: [], skills: [], permissions: ['read', 'write', 'host_command'] };
        state.drafts.set(task.task_id, {
          task_id: task.task_id, draft_id: 'audit-draft', source_checkpoint_id: 'checkpoint-audit',
          system_prompt: 'Preserve constraints.',
          history_messages: [{ role: 'human', content: '检查工作区' }, { role: 'ai', content: '已检查' }],
          final_human_message: '完成实现并验证。', token_estimate: 720,
          equipment: { model_name: 'local-model', skills: [], permissions: ['read'] },
        });
        state.view = 'draft'; render();
      } else if (target === 'agents') {
        state.agents.set(task.task_id, [{ agent_id: 'agent-audit', permissions: ['read'], checkpoint_ns: 'audit', latest_run: { status: 'success' } }]);
        state.agentDialog = { agentId: null, messages: [], busy: false };
        state.view = 'focus'; state.inspector.open = true; state.inspector.tab = 'agents'; render();
      } else if (target === 'commitment') {
        state.view = 'focus'; state.commitment.taskId = task.task_id; state.commitment.stage = 4; render();
        appendCommitmentTrace({ actor: 'supervisor', stage: 4, attempt: 1, title: '范围审查', status: 'completed', detail: '确认边界。', payload: { scope: 'desktop' } });
        showReview({ stage: 4, draft: { summary: '保持同源并修复稳定性。' }, allowed_decisions: ['approve', 'revise'] });
      } else if (target === 'compression') {
        state.compression = {
          taskId: task.task_id, request: { usage: 28640, limit: 32000, ratio: .9 },
          messages: [
            { id: 'c1', role: 'human', content: '检查前端。' },
            { id: 'c2', role: 'ai', content: '确认约束。' },
            { id: 'c3', role: 'tool', content: 'loaded' },
          ], selected: new Set([0, 1]), ranges: [], busy: false, recovery: null,
        };
        state.view = 'compress'; render();
      } else if (target === 'materials') {
        state.materials.set(task.task_id, [
          { material_id: 'm1', relative_path: 'specs/product-direction.md', reading_mode: 'full', instruction_mode: 'strict', retention: 'irreplaceable', needs_confirmation: true },
          { material_id: 'm2', relative_path: 'research/notes.txt', reading_mode: 'rough', instruction_mode: 'reference', retention: 'removable', needs_confirmation: false },
        ]);
        state.view = 'focus'; state.inspector.open = true; state.inspector.tab = 'materials'; render();
      } else if (target === 'plugins') {
        state.plugins = {
          filter: 'all', selectedName: 'spatial-patrol',
          plugins: [
            { name: 'spatial-patrol', version: '0.1.0', status: 'active', injected: ['service.spatial'], requires: [] },
            { name: 'broken', version: '0.1.0', status: 'rejected', injected: [], requires: [], conflict: { interface: 'tool', current: 'demo', new: 'broken' } },
          ],
          interfaces: { 'service.spatial': { plugins: ['spatial-patrol'], cardinality: 'single', read_only: true } },
          traces: [{ interface: 'service.spatial', plugin: 'spatial-patrol', status: 'failed', duration_ms: 4, error: '文件被占用' }],
        };
        state.view = 'plugins'; render();
      } else if (target === 'file') {
        const material = { material_id: 'm-file', relative_path: 'specs/product-direction.md', reading_mode: 'full', instruction_mode: 'strict', retention: 'irreplaceable' };
        state.materials.set(task.task_id, [material]);
        state.filesPanel = material;
        state.panelWidth = 520;
        state.view = 'focus'; render();
      } else if (target === 'dialog') {
        state.view = 'focus'; render();
        document.querySelector('#taskDialog')?.showModal();
      } else if (target === 'error') {
        state.view = 'focus';
        const detail = state.details.get(task.task_id);
        detail.messages = [
          { id: 'audit-human', role: 'human', content: '检查失败恢复。' },
          { role: 'ai', content: '', tool_calls: [{ id: 'audit-error', name: 'web_search', args: { query: 'Focus' } }] },
          { role: 'tool', name: 'web_search', tool_call_id: 'audit-error', status: 'error', content: '搜索服务暂时不可用，请稍后重试。' },
        ];
        render();
      }
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    };
  })()`);
  if (qaDir) {
    win.showInactive();
    await wait(100);
  }

  const failures = [];
  for (const item of cases) {
    win.setSize(item.width, item.height);
    win.webContents.setZoomFactor(item.zoom);
    await wait(80);
    for (const target of targets) {
      await win.webContents.executeJavaScript(`window.__f20PrepareAuditTarget(${JSON.stringify(target)})`);
      await wait(35);
      const result = await win.webContents.executeJavaScript(`(() => {
        const rendered = [...document.querySelectorAll('body *')];
        const visible = node => {
          if (node.closest('[hidden]')) return false;
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility === 'hidden') return false;
          const rect = node.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };
        const ids = rendered.filter(node => node.id).map(node => node.id);
        const unnamed = [...document.querySelectorAll('button')].filter(button => visible(button) && !(button.textContent.trim() || button.getAttribute('aria-label') || button.title));
        const brokenControls = [...document.querySelectorAll('[aria-controls]')].filter(node => {
          const id = node.getAttribute('aria-controls');
          return id && !document.getElementById(id);
        });
        const nestedInteractive = [...document.querySelectorAll('button button, button a, a button, a a')].filter(visible);
        const focusableZero = [...document.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])')]
          .filter(node => visible(node) && (node.getBoundingClientRect().width < 1 || node.getBoundingClientRect().height < 1));
        const appRect = document.querySelector('#app').getBoundingClientRect();
        const workspaceRect = document.querySelector('.app-workspace').getBoundingClientRect();
        const mapCards = [...document.querySelectorAll('.map-root-group .task-card-shell')].slice(0, 2).map(node => node.getBoundingClientRect());
        const focusShell = document.querySelector('.focus-shell');
        const filePanel = document.querySelector('.file-panel');
        return {
          duplicateIds: ids.length - new Set(ids).size,
          unnamed: unnamed.length,
          brokenControls: brokenControls.length,
          nestedInteractive: nestedInteractive.length,
          focusableZero: focusableZero.length,
          openDialogs: document.querySelectorAll('dialog[open]').length,
          documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
          appWidth: appRect.width,
          appHeight: appRect.height,
          duplicateWorkspaceHeaders: document.querySelectorAll('.workspace-context, #workspaceKicker, #workspaceTitle, #workspaceMeta').length,
          workspaceContentTopDelta: Math.abs(appRect.top - workspaceRect.top),
          duplicateContentTitles: document.querySelectorAll('.map-toolbar .workspace-kicker, .draft-heading h1, .compression-heading h1').length,
          mapToolbarHeight: document.querySelector('.map-toolbar')?.getBoundingClientRect().height || 0,
          mapCardsSameRow: mapCards.length === 2 && Math.abs(mapCards[0].top - mapCards[1].top) < 2,
          contextEditorColumns: document.querySelector('.context-editor-view') ? getComputedStyle(document.querySelector('.context-editor-view')).gridTemplateColumns : '',
          pluginsWorkbenchColumns: document.querySelector('.plugins-workbench') ? getComputedStyle(document.querySelector('.plugins-workbench')).gridTemplateColumns : '',
          fileFocusDisplay: document.querySelector('.focus-view') ? getComputedStyle(document.querySelector('.focus-view')).display : '',
          filePanelWidthDelta: focusShell && filePanel ? Math.abs(focusShell.getBoundingClientRect().width - filePanel.getBoundingClientRect().width) : 0,
          errors: window.__f20AuditErrors.splice(0),
        };
      })()`);
      const problems = Object.entries(result)
        .filter(([key, value]) => !["appWidth", "appHeight", "errors", "openDialogs", "duplicateContentTitles", "mapToolbarHeight", "mapCardsSameRow", "contextEditorColumns", "pluginsWorkbenchColumns", "fileFocusDisplay", "filePanelWidthDelta"].includes(key) && Number(value) > 0)
        .map(([key, value]) => `${key}=${value}`);
      if (target === "dialog" ? result.openDialogs !== 1 : result.openDialogs !== 0) problems.push(`openDialogs=${result.openDialogs}`);
      if (result.appWidth < 300 || result.appHeight < 220) problems.push(`app=${result.appWidth}x${result.appHeight}`);
      if (["map", "draft", "compression"].includes(target) && result.duplicateContentTitles) problems.push(`duplicateContentTitles=${result.duplicateContentTitles}`);
      if (target === "map" && result.mapToolbarHeight > 48) problems.push(`mapToolbarHeight=${result.mapToolbarHeight}`);
      if (item.width === 900 && item.zoom === 1.5 && target === "map" && result.mapCardsSameRow) problems.push("mapCards=still-two-columns");
      if (item.width === 900 && item.zoom === 1.5 && target === "context" && result.contextEditorColumns.trim().split(/\s+/).length !== 1) problems.push(`contextColumns=${result.contextEditorColumns}`);
      if (item.width === 900 && item.zoom === 1.5 && target === "plugins" && result.pluginsWorkbenchColumns.trim().split(/\s+/).length !== 1) problems.push(`pluginColumns=${result.pluginsWorkbenchColumns}`);
      if ((item.width / item.zoom) <= 1100 && target === "file" && (result.fileFocusDisplay !== "none" || result.filePanelWidthDelta > 1)) problems.push(`fileSingleSurface=${result.fileFocusDisplay}/${result.filePanelWidthDelta}`);
      if (result.errors.length) problems.push(`errors=${result.errors.join("|")}`);
      if (problems.length) failures.push(`${target} ${item.width}x${item.height}@${item.zoom}: ${problems.join(", ")}`);
      if (qaDir && (captureAllViewports || (item.width === 1440 && item.height === 1024 && item.zoom === 1))) {
        await wait(90);
        const image = await win.webContents.capturePage();
        const viewportPrefix = captureAllViewports
          ? `${item.width}x${item.height}-z${String(item.zoom).replace(".", "_")}-`
          : "";
        fs.writeFileSync(path.join(qaDir, `${viewportPrefix}${String(targets.indexOf(target) + 1).padStart(2, "0")}-${target}.png`), image.toPNG());
      }
    }
  }

  win.destroy();
  if (rendererErrors.length) failures.push(`renderer console: ${rendererErrors.join(" | ")}`);
  if (failures.length) throw new Error(`F20 view audit:\n- ${failures.join("\n- ")}`);
  console.log(`f20-view-audit: ${targets.length} 类状态 × ${cases.length} 组尺寸/缩放通过`);
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
