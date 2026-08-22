/* Exercises the real local splash DOM, motion contract, stage bridge and visual capture in Electron. */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

const userData = path.join(os.tmpdir(), `focus-f20-splash-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 440,
    height: 340,
    frame: false,
    transparent: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
  });
  await win.loadFile(path.join(__dirname, "splash.html"));

  const initial = await win.webContents.executeJavaScript(`(() => ({
    label: document.querySelector('.splash-card')?.getAttribute('aria-label'),
    duplicateBrandCopy: Boolean(document.querySelector('.splash-copy, .splash-kicker, h1')),
    statusRole: document.querySelector('#splashStatus')?.getAttribute('role'),
    imageLoaded: Boolean(document.querySelector('.splash-mark img')?.complete && document.querySelector('.splash-mark img')?.naturalWidth),
    mark: (() => { const rect = document.querySelector('.splash-mark')?.getBoundingClientRect(); return rect ? { width: rect.width, height: rect.height } : null; })(),
    animations: document.getAnimations({ subtree: true }).map(item => item.animationName).filter(Boolean),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }))()`);
  if (initial.label !== "Focus 正在启动" || initial.duplicateBrandCopy || initial.statusRole !== "status" || !initial.imageLoaded || initial.mark?.width < 100 || initial.overflow > 1) {
    throw new Error(`启动页基本结构异常: ${JSON.stringify(initial)}`);
  }
  if (!initial.animations.some(name => name === "focus-arrive" || name === "focus-float")) {
    throw new Error(`Focus 图标启动动画未运行: ${initial.animations.join(", ")}`);
  }

  const stage = await win.webContents.executeJavaScript(`(() => {
    window.setFocusSplashStage('正在加载工作区', 84);
    return {
      label: document.querySelector('#splashStatus').textContent,
      progress: document.querySelector('#splashProgress').style.getPropertyValue('--splash-progress'),
    };
  })()`);
  if (stage.label !== "正在加载工作区" || stage.progress !== "84%") throw new Error(`启动阶段桥接失败: ${JSON.stringify(stage)}`);

  await wait(850);
  const visualState = await win.webContents.executeJavaScript(`(() => {
    const mark = document.querySelector('.splash-mark');
    const image = mark.querySelector('img');
    return {
      markOpacity: getComputedStyle(mark).opacity,
      imageOpacity: getComputedStyle(image).opacity,
      markAnimations: mark.getAnimations().map(item => ({ name: item.animationName, time: item.currentTime, playState: item.playState })),
    };
  })()`);
  if (Number(visualState.markOpacity) < 0.95 || Number(visualState.imageOpacity) < 0.95) {
    throw new Error(`启动图标动画结束后仍然偏淡: ${JSON.stringify(visualState)}`);
  }
  const qaDir = path.join(
    __dirname,
    "..",
    "openspec",
    "changes",
    "archive",
    "2026-08-22-f22-premium-interaction-system",
    "qa",
    "after",
    "branding",
  );
  fs.mkdirSync(qaDir, { recursive: true });
  fs.writeFileSync(path.join(qaDir, "focus-splash.png"), (await win.webContents.capturePage()).toPNG());
  win.setContentSize(759, 759);
  await wait(80);
  fs.writeFileSync(path.join(qaDir, "focus-splash-759.png"), (await win.webContents.capturePage()).toPNG());

  await win.webContents.debugger.attach("1.3");
  await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", {
    media: "screen",
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  await win.reload();
  const reduced = await win.webContents.executeJavaScript(`getComputedStyle(document.querySelector('.splash-mark')).animationName`);
  if (reduced !== "none") throw new Error(`减少动态效果没有关闭动画: ${reduced}`);

  console.log("f20-splash: 图标动画、启动阶段、视觉快照与 reduced-motion 通过");
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
