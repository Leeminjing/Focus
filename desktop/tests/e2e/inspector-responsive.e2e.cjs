/* 使用生产 HTML/CSS 隔离验证检查器断点和窄屏导航，无后端依赖。
 * 运行：electron tests/e2e/inspector-responsive.e2e.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow } = require("electron");

const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "focus-inspector-responsive-"));
app.setPath("userData", path.join(fixtureDir, "user-data"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.on("window-all-closed", () => {});

async function run() {
  const indexPath = path.resolve(__dirname, "../../src/renderer/index.html");
  const html = fs.readFileSync(indexPath, "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace("<head>", `<head><base href="${pathToFileURL(indexPath).href}">`);
  const fixturePath = path.join(fixtureDir, "shell.html");
  fs.writeFileSync(fixturePath, html);
  const i18nSource = fs.readFileSync(path.resolve(__dirname, "../../src/renderer/shared/i18n/i18n.js"), "utf8");
  const screenshotDir = process.env.FOCUS_RESPONSIVE_SCREENSHOTS;
  if (screenshotDir) fs.mkdirSync(screenshotDir, { recursive: true });
  const sizes = [1440, 1101, 1100, 1000, 901, 900, 641, 640, 600].map(width => [width, 1]);
  sizes.push([900, 1.5]);
  for (const [windowWidth, zoom] of sizes) {
    const width = windowWidth / zoom;
    const win = new BrowserWindow({ width: windowWidth, height: 800, useContentSize: true, show: false, webPreferences: { backgroundThrottling: false } });
    try {
      await win.loadFile(fixturePath);
      win.webContents.setZoomFactor(zoom);
      await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
      await win.webContents.executeJavaScript(i18nSource);
      for (const [mode, locale] of [["default", "zh-CN"], ["customized", "zh-CN"], ["collapsed", "zh-CN"], ["default", "en-US"]]) {
        const result = await win.webContents.executeJavaScript(`(() => {
          window.FocusI18n.setLocale('${locale}');
          window.FocusI18n.apply(document);
          const shell = document.querySelector('.app-shell');
          shell.style.removeProperty('--shell-nav-width');
          shell.style.removeProperty('--inspector-width');
          shell.classList.toggle('is-nav-collapsed', ${mode === "collapsed"});
          if (${mode !== "default"}) {
            shell.style.setProperty('--shell-nav-width', '${mode === "collapsed" ? 0 : 280}px');
            shell.style.setProperty('--inspector-width', '480px');
          }
          const workspace = document.querySelector('.app-workspace');
          const inspector = document.querySelector('#appInspector');
          const resizer = document.querySelector('.shell-resizer-inspector');
          inspector.hidden = true;
          const closedWidth = workspace.getBoundingClientRect().width;
          inspector.hidden = false;
          const openedWidth = workspace.getBoundingClientRect().width;
          const rect = inspector.getBoundingClientRect();
          return {
            viewport: innerWidth, closedWidth, openedWidth,
            inspectorWidth: rect.width, left: rect.left, right: rect.right,
            position: getComputedStyle(inspector).position,
            resizerWidth: resizer.getBoundingClientRect().width,
            scrollWidth: document.documentElement.scrollWidth,
            navigation: [...document.querySelectorAll('.app-nav-item')].map(button => {
              const label = button.querySelector('.app-nav-label');
              const labelRect = label.getBoundingClientRect();
              const buttonRect = button.getBoundingClientRect();
              return {
                name: label.textContent.trim(),
                visible: labelRect.width > 0 && labelRect.height > 0 && getComputedStyle(label).visibility !== 'hidden',
                fits: labelRect.left >= buttonRect.left && labelRect.right <= buttonRect.right + 1
                  && labelRect.top >= buttonRect.top && labelRect.bottom <= buttonRect.bottom + 1,
              };
            }),
          };
        })()`);
        const label = `${width}px ${mode} ${locale}: ${JSON.stringify(result)}`;
        assert.equal(result.viewport, width, label);
        assert.ok(result.inspectorWidth > 0, `检查器不可见 ${label}`);
        if (width <= 1100) {
          assert.ok(Math.abs(result.closedWidth - result.openedWidth) <= 1, `浮层挤占工作区 ${label}`);
          assert.equal(result.position, "absolute", label);
          assert.equal(result.resizerWidth, 0, `浮层显示拖拽手柄 ${label}`);
        } else {
          assert.ok(Math.abs(result.closedWidth - result.openedWidth - result.inspectorWidth) <= 1, `宽屏未停靠 ${label}`);
          assert.ok(result.resizerWidth > 0, `停靠手柄不可见 ${label}`);
        }
        assert.ok(result.left >= -1 && result.right <= width + 1, `检查器超出窗口 ${label}`);
        assert.ok(result.scrollWidth <= width + 1, `页面横向溢出 ${label}`);
        if (mode !== "collapsed") {
          assert.equal(result.navigation.length, 7, `导航入口缺失 ${label}`);
          for (const entry of result.navigation) {
            assert.ok(entry.name && entry.visible, `导航名称不可见 ${entry.name} ${width}px ${mode}`);
            assert.ok(entry.fits, `导航名称超出按钮 ${entry.name} ${width}px ${mode}`);
          }
        }
        if (screenshotDir && width <= 640 && mode === "default") {
          await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
          const screenshot = await win.webContents.capturePage();
          fs.writeFileSync(path.join(screenshotDir, `navigation-${windowWidth}-${zoom}-${locale}.png`), screenshot.toPNG());
        }
      }
    } finally {
      win.destroy();
    }
  }
  console.log("inspector-responsive: 10 组尺寸/缩放 × 中文默认/自定义/折叠及英文默认，共 40 组通过，导航名称可见且未溢出");
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
