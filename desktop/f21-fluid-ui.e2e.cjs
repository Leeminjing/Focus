/*
 * 本文件以真实 Electron 验证 F21 主会话的流动密度。输入为确定性的短/长用户消息、reasoning、
 * pending/success/error 工具事件与展开详情，输出为内容驱动尺寸、状态邻近、轻量表面、可访问 disclosure、
 * 页面溢出和 QA 截图；工作流不调用后端或改变用户数据。示例：`node f21-fluid-ui.e2e.cjs`。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-f21-fluid-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const qaDir = path.join(__dirname, "..", "openspec", "changes", "f21-fluid-ui-system", "qa", "after");
fs.mkdirSync(qaDir, { recursive: true });

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 1200,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 120; count += 1) {
      if (document.querySelector('.focus-view')) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('Focus 启动超时'));
  })`);
  await new Promise(resolve => setTimeout(resolve, 180));

  const result = await win.webContents.executeJavaScript(`(() => {
    state.inspector.open = false;
    state.view = 'focus';
    state.activeTaskId = 'child';
    const detail = state.details.get('child');
    detail.messages = [
      { id: 'short', role: 'human', content: 'readme是不是也要改改' },
      { id: 'long', role: 'human', content: '请检查 C:/workspace/a/very/long/path/that/must/wrap/without/creating/page/overflow/README.md，并说明需要修改的内容。' },
      { role: 'ai', content: '', reasoning_content: '先读取中文和英文 README，再比较当前实现与文档描述。', tool_calls: [
        { id: 'pending', name: 'list_files', args: { path: 'docs' } },
        { id: 'success', name: 'read_file', args: { path: 'README.md' } },
        { id: 'error', name: 'edit_file', args: { path: 'C:/outside/README.md' } },
      ] },
      { role: 'tool', name: 'read_file', tool_call_id: 'success', status: 'success', content: 'README content' },
      { role: 'tool', name: 'edit_file', tool_call_id: 'error', status: 'error', content: '路径不属于当前工作区: C:/outside/README.md' },
      { role: 'ai', content: '需要同步更新中英文 README，并保留错误恢复说明。' },
    ];
    render();
    const humans = [...document.querySelectorAll('.work-record.human')];
    const eventNodes = [...document.querySelectorAll('.conversation-event')];
    const eventMetrics = eventNodes.map(node => {
      const summary = node.querySelector('summary');
      const preview = node.querySelector('.conversation-event-preview');
      const status = node.querySelector('.conversation-event-status');
      const rect = node.getBoundingClientRect();
      const summaryRect = summary.getBoundingClientRect();
      const previewRect = preview?.getBoundingClientRect();
      const statusRect = status?.getBoundingClientRect();
      return {
        width: rect.width,
        height: summaryRect.height,
        statusGap: previewRect && statusRect ? statusRect.left - previewRect.right : 0,
        text: summary.textContent.trim(),
      };
    });
    for (const node of eventNodes) node.open = true;
    const focus = document.querySelector('.focus-view');
    const composer = document.querySelector('.composer-shell');
    return {
      shortWidth: humans[0].getBoundingClientRect().width,
      longWidth: humans[1].getBoundingClientRect().width,
      conversationWidth: document.querySelector('.conversation').getBoundingClientRect().width,
      eventMetrics,
      eventCount: eventNodes.length,
      openPreCount: document.querySelectorAll('.conversation-event[open] pre').length,
      focusBorder: getComputedStyle(focus).borderTopWidth,
      composerShadow: getComputedStyle(composer).boxShadow,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      unnamedButtons: [...document.querySelectorAll('button')].filter(button => !(button.textContent.trim() || button.getAttribute('aria-label') || button.title)).length,
    };
  })()`);

  // 等待两次绘制，避免 capturePage 读取 render() 之前的合成帧；随后固定到会话顶部。
  await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
  await win.webContents.executeJavaScript(`document.querySelector('.conversation').scrollTop = 0`);
  win.showInactive();
  await new Promise(resolve => setTimeout(resolve, 80));

  const image = await win.webContents.capturePage();
  fs.writeFileSync(path.join(qaDir, "f21-conversation-1200x800.png"), image.toPNG());
  win.hide();
  win.destroy();

  const failures = [];
  if (result.shortWidth >= 260) failures.push(`短消息仍过宽: ${result.shortWidth}`);
  if (result.longWidth <= result.shortWidth || result.longWidth > result.conversationWidth * 0.82) failures.push(`长消息尺寸异常: ${result.longWidth}/${result.conversationWidth}`);
  if (result.eventCount !== 4) failures.push(`逻辑事件数量错误: ${result.eventCount}`);
  for (const metric of result.eventMetrics) {
    if (metric.width > result.conversationWidth * 0.86) failures.push(`事件仍占整行: ${metric.text} ${metric.width}/${result.conversationWidth}`);
    if (metric.height > 28) failures.push(`事件折叠行过高: ${metric.text} ${metric.height}`);
    if (metric.statusGap > 20) failures.push(`状态离内容过远: ${metric.text} gap=${metric.statusGap}`);
  }
  if (result.openPreCount < 4) failures.push(`展开详情不完整: ${result.openPreCount}`);
  if (result.focusBorder !== "0px") failures.push(`Focus 仍使用大面板边框: ${result.focusBorder}`);
  if (result.composerShadow !== "none") failures.push(`Composer 仍使用常驻阴影: ${result.composerShadow}`);
  if (result.documentOverflow > 1 || result.unnamedButtons) failures.push(`溢出或无名控件: ${JSON.stringify(result)}`);
  if (failures.length) throw new Error(`F21 fluid audit:\n- ${failures.join("\n- ")}`);
  console.log("f21-fluid-ui-e2e: 用户气泡、四类事件、展开详情、轻量表面与溢出通过");
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
