/*
 * 本文件以真实 Electron 验证 F20 响应式与键盘基线。输入为确定性测试 preload、窗口尺寸和
 * 100%/125%/150% 缩放，输出为无页面横向溢出、关键动作可见、语义名称、Enter/Space 与 Escape 断言。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-f20-responsive-${process.pid}`);
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

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function run() {
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
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 120; count += 1) {
      if (document.querySelector('.focus-view')) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('Focus 启动超时'));
  })`);
  win.showInactive();

  for (const item of cases) {
    win.setSize(item.width, item.height);
    win.webContents.setZoomFactor(item.zoom);
    await wait(140);
    const failures = await win.webContents.executeJavaScript(`(() => {
      state.view = 'focus';
      state.inspector.open = false;
      state.filesPanel = null;
      render();
      const failures = [];
      const viewport = { width: innerWidth, height: innerHeight };
      const visible = (node) => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.left >= -1 && rect.right <= innerWidth + 1 && rect.top >= -1 && rect.bottom <= innerHeight + 1;
      };
      if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) failures.push('document 横向溢出');
      if (document.body.scrollWidth > document.body.clientWidth + 1) failures.push('body 横向溢出');
      if (!visible(document.querySelector('[data-action="new-task"]'))) failures.push('新增任务不可见');
      if (!visible(document.querySelector('[data-action="send-main"]'))) failures.push('发送动作不可见');
      if (!visible(document.querySelector('.composer-shell'))) failures.push('Composer 被裁切');
      const unnamed = [...document.querySelectorAll('button:not([hidden])')].filter(button => {
        if (button.closest('[hidden]')) return false;
        const style = getComputedStyle(button);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        return !(button.textContent.trim() || button.getAttribute('aria-label') || button.title);
      });
      if (unnamed.length) failures.push('存在无可访问名称按钮:' + unnamed.length);
      const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
      if (new Set(ids).size !== ids.length) failures.push('存在重复 id');
      const current = document.querySelectorAll('.app-nav-item[aria-current="page"]');
      if (current.length !== 1) failures.push('全局导航 aria-current 不唯一');
      if (!getComputedStyle(document.querySelector('[data-action="show-map"]')).minHeight) failures.push('导航控制无尺寸');
      return { failures, viewport };
    })()`);
    if (failures.failures.length) {
      throw new Error(`${item.width}x${item.height}@${item.zoom}: ${failures.failures.join("；")} (CSS viewport ${failures.viewport.width}x${failures.viewport.height})`);
    }
  }

  win.setSize(900, 680);
  win.webContents.setZoomFactor(1);
  await wait(100);
  const nativeActivation = await win.webContents.executeJavaScript(`(async () => {
    const map = document.querySelector('[data-action="show-map"]');
    const contexts = document.querySelector('[data-action="show-contexts"]');
    if (map.tagName !== 'BUTTON' || contexts.tagName !== 'BUTTON' || map.disabled || contexts.disabled) return false;
    map.focus();
    map.click();
    if (state.view !== 'map') return false;
    contexts.focus();
    contexts.click();
    await new Promise(resolve => requestAnimationFrame(resolve));
    return !document.querySelector('#appInspector').hidden;
  })()`);
  if (!nativeActivation) throw new Error("Enter/Space 所需的原生 button 语义或激活路径失效");
  await win.webContents.executeJavaScript(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
  await wait(80);
  const escape = await win.webContents.executeJavaScript(`({ hidden: document.querySelector('#appInspector').hidden, action: document.activeElement?.dataset?.action })`);
  if (!escape.hidden || escape.action !== "show-contexts") throw new Error("Escape 未关闭 Inspector 或未归还焦点");

  console.log("f20-responsive: 5 组尺寸/缩放、原生 Enter/Space 语义与 Escape 验证通过");
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
