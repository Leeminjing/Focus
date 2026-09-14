/*
 * 本文件以真实 Electron 复现 F20 隐蔽稳定性缺陷。输入为确定性 preload 和真实 DOM 事件，输出为
 * Composer 状态、对话框取消、Inspector roving focus、文件面板边界、未知视图回退及全局 DOM 守卫。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-f20-stability-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function evaluate(win, source) {
  return win.webContents.executeJavaScript(source, true);
}

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 900,
    height: 680,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  await win.loadFile(path.join(__dirname, "index.html"));
  await evaluate(win, `new Promise(async (resolve, reject) => {
    for (let count = 0; count < 120; count += 1) {
      if (document.querySelector('.focus-view')) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('Focus 启动超时'));
  })`);

  const failures = [];

  const composer = await evaluate(win, `(async () => {
    const input = document.querySelector('#mainInput');
    input.value = '切换视图后必须保留的未发送内容';
    document.querySelector('[data-action="show-plugins"]').click();
    await new Promise(resolve => setTimeout(resolve, 80));
    return state.details.get(state.activeTaskId)?.ui_state?.input || '';
  })()`);
  if (composer !== "切换视图后必须保留的未发送内容") failures.push("进入插件中心会丢失未发送 Composer 内容");

  const cancel = await evaluate(win, `(async () => {
    state.view = 'focus'; render();
    document.querySelector('[data-action="new-task"]').click();
    document.querySelector('#workspacePath').value = 'C:/workspace/cancel-must-not-create';
    document.querySelector('#threadTitle').value = '取消不得创建';
    const before = state.tasks.length;
    document.querySelector('#taskDialog button[value="cancel"]').click();
    await new Promise(resolve => setTimeout(resolve, 60));
    return { open: document.querySelector('#taskDialog').open, before, after: state.tasks.length };
  })()`);
  if (cancel.open || cancel.after !== cancel.before) failures.push("新建任务取消没有无副作用地关闭对话框");

  const inspectorFocus = await evaluate(win, `(async () => {
    state.view = 'focus'; state.inspector.open = false; render();
    document.querySelector('[data-action="show-contexts"]').click();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const first = document.querySelector('[role="tab"][data-inspector-tab="context"]');
    first.focus();
    first.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return { tab: document.activeElement?.dataset?.inspectorTab || '', selected: document.activeElement?.getAttribute?.('aria-selected') || '' };
  })()`);
  if (inspectorFocus.tab !== "materials" || inspectorFocus.selected !== "true") failures.push("Inspector 方向键切换后焦点被容器偷走");

  const panel = await evaluate(win, `(() => {
    state.inspector.open = false;
    state.view = 'focus';
    state.panelWidth = 9999;
    state.filesPanel = { relative_path: 'README.md', path: 'README.md' };
    render();
    const mainNode = document.querySelector('.focus-view');
    const main = mainNode.getBoundingClientRect();
    const file = document.querySelector('.file-panel').getBoundingClientRect();
    const shell = document.querySelector('.focus-shell').getBoundingClientRect();
    return {
      mainWidth: main.width,
      mainDisplay: getComputedStyle(mainNode).display,
      fileWidth: file.width,
      shellWidth: shell.width,
      viewport: innerWidth,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      stored: state.panelWidth,
    };
  })()`);
  if (panel.mainDisplay !== "none" || Math.abs(panel.fileWidth - panel.shellWidth) > 1 || panel.overflow > 1 || !Number.isFinite(panel.stored)) {
    failures.push(`中小窗口文件工作台没有保持单工作面：${JSON.stringify(panel)}`);
  }

  const fallback = await evaluate(win, `(() => {
    state.filesPanel = null;
    state.view = 'missing-plugin-view';
    try { render(); return { threw: false, view: state.view, hasFocus: Boolean(document.querySelector('.focus-view')) }; }
    catch (error) { return { threw: true, message: error.message }; }
  })()`);
  if (fallback.threw || fallback.view !== "focus" || !fallback.hasFocus) failures.push(`未知插件视图没有安全回退：${JSON.stringify(fallback)}`);

  const compression = await evaluate(win, `(async () => {
    state.filesPanel = null;
    state.compression = {
      taskId: state.activeTaskId,
      messages: [
        { id: 'm1', role: 'human', content: '可以直接删除的消息' },
        { id: 'm2', role: 'ai', content: '可以加入摘要计划的消息' },
      ],
      selected: new Set(), ranges: [], request: { usage: 80, limit: 100 },
    };
    state.view = 'compress';
    render();
    document.querySelector('[data-action="toggle-compress-message"][data-index="0"]').click();
    document.querySelector('[data-action="compression-delete-selection"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    const deleted = JSON.parse(JSON.stringify(state.compression.ranges));
    const firstDisabled = document.querySelector('[data-action="toggle-compress-message"][data-index="0"]').disabled;
    document.querySelector('[data-action="toggle-compress-message"][data-index="1"]').click();
    document.querySelector('[data-action="compression-join-selection"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    const uniqueIds = state.compression.ranges.flatMap(range => range.source_ids);
    const readyBeforeSummary = compressionReady();
    state.view = 'focus'; render();
    return { deleted, firstDisabled, uniqueIds, readyBeforeSummary };
  })()`);
  if (!compression.deleted[0]?.delete || !compression.firstDisabled || new Set(compression.uniqueIds).size !== compression.uniqueIds.length || compression.readyBeforeSummary) {
    failures.push(`压缩计划允许重复来源或直接删除不可用：${JSON.stringify(compression)}`);
  }

  win.webContents.setZoomFactor(1.5);
  const compactConversation = await evaluate(win, `(() => {
    state.view = 'focus';
    const detail = state.details.get(state.activeTaskId);
    detail.messages = [
      { role: 'human', content: '读取外部路径' },
      { role: 'ai', content: '', reasoning_content: '先检查路径是否位于工作区', tool_calls: [{ id: 'outside-1', name: 'read_file', args: { path: 'C:/outside/a/very/long/path/requirements.md' } }] },
      { role: 'tool', name: 'read_file', tool_call_id: 'outside-1', status: 'error', content: '路径不属于当前工作区: C:/outside/a/very/long/path/requirements.md' },
      { role: 'ai', content: '该路径不在当前工作区，请提供工作区内路径。' },
    ];
    render();
    const conversation = document.querySelector('#conversation');
    const events = [...conversation.querySelectorAll('.conversation-event')];
    const text = conversation.textContent;
    const rect = events[1]?.getBoundingClientRect();
    events[1]?.querySelector('summary')?.click();
    return {
      count: events.length,
      hasFailure: text.includes('失败') && text.includes('路径不属于当前工作区'),
      hasLegacyRole: /YOU|你的指令|FOCUS|助手/.test(text),
      hasDetails: Boolean(events[1]?.querySelector('pre')),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      eventRight: rect?.right || 0,
      viewport: innerWidth,
    };
  })()`);
  if (compactConversation.count !== 2 || !compactConversation.hasFailure || compactConversation.hasLegacyRole || !compactConversation.hasDetails || compactConversation.overflow > 1 || compactConversation.eventRight > compactConversation.viewport + 1) {
    failures.push(`900×680@150% 紧凑会话事件失败：${JSON.stringify(compactConversation)}`);
  }
  win.webContents.setZoomFactor(1);

  const dom = await evaluate(win, `(() => {
    const visible = node => {
      const style = getComputedStyle(node);
      return !node.closest('[hidden]') && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
    const unnamed = [...document.querySelectorAll('button')].filter(button => visible(button) && !(button.textContent.trim() || button.getAttribute('aria-label') || button.title));
    const brokenControls = [...document.querySelectorAll('[aria-controls]')].filter(node => {
      const id = node.getAttribute('aria-controls');
      return id && !document.getElementById(id);
    });
    const header = document.querySelector('.app-header');
    const appMark = document.querySelector('.app-mark img');
    const taskContext = document.querySelector('.shell-task-context');
    const headerRect = header?.getBoundingClientRect();
    const appMarkRect = appMark?.getBoundingClientRect();
    const taskRect = taskContext?.getBoundingClientRect();
    return {
      duplicateIds: ids.length - new Set(ids).size,
      unnamed: unnamed.length,
      brokenControls: brokenControls.length,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      hasBrand: Boolean(document.querySelector('.brand')),
      appMark: { naturalWidth: appMark?.naturalWidth || 0, width: appMarkRect?.width || 0, height: appMarkRect?.height || 0 },
      header: { height: headerRect?.height || 0, drag: header ? getComputedStyle(header).webkitAppRegion : '' },
      taskContext: { x: taskRect?.x || 0, width: taskRect?.width || 0, display: taskContext ? getComputedStyle(taskContext).display : '' },
    };
  })()`);
  if (dom.duplicateIds || dom.unnamed || dom.brokenControls || dom.documentOverflow > 1 || dom.hasBrand || dom.appMark.naturalWidth < 1 || dom.appMark.width < 30 || dom.header.height < 52 || (dom.taskContext.display !== 'none' && dom.taskContext.width < 120)) failures.push(`全局 DOM 守卫失败：${JSON.stringify(dom)}`);

  win.destroy();
  if (failures.length) throw new Error(`F20 stability audit:\n- ${failures.join("\n- ")}`);
  console.log("f20-stability: Composer、取消、焦点、面板、回退、压缩计划与 DOM 守卫通过");
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
