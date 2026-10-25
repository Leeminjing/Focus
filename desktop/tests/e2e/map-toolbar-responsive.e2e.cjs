/* 使用 renderMap 的真实 HTML 与生产 CSS 验证工具栏按实际空间换行。
 * 运行：electron tests/e2e/map-toolbar-responsive.e2e.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow } = require("electron");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "focus-map-toolbar-responsive-"));
app.setPath("userData", path.join(fixtureDir, "user-data"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.on("window-all-closed", () => {});

async function run() {
  const renderer = path.resolve(__dirname, "../../src/renderer");
  const html = fs.readFileSync(path.join(renderer, "index.html"), "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace("<head>", `<head><base href="${pathToFileURL(path.join(renderer, "index.html")).href}">`);
  const fixturePath = path.join(fixtureDir, "map.html");
  fs.writeFileSync(fixturePath, html);
  const appNode = { dataset: {}, innerHTML: "", replaceChildren() {} };
  const harness = createAppHarness({ globals: { setTimeout }, selectors: { "#app": appNode } });
  for (const source of ["shared/i18n/i18n-catalog.js", "shared/i18n/i18n.js"]) {
    harness.vm.runInContext(fs.readFileSync(path.join(renderer, source), "utf8"), harness.context);
  }
  harness.vm.runInContext(readAppSource(), harness.context);
  let count = 0;
  for (const viewport of [1440, 800]) {
    const win = new BrowserWindow({ width: viewport, height: 1000, useContentSize: true, show: false, webPreferences: { backgroundThrottling: false } });
    try {
      await win.loadFile(fixturePath);
      for (const locale of ["zh-CN", "en-US"]) {
        for (const selection of [false, true]) {
          for (const mode of ["tree", "cards"]) {
            appNode.innerHTML = "";
            harness.vm.runInContext(`FocusI18n.setLocale('${locale}'); state.mapViewMode = '${mode}'; state.selectionMode = ${selection}; state.soldierArmed = true; renderMap();`, harness.context);
            for (const width of [Math.min(1200, viewport - 32), 520, 360, 280]) {
              const result = await win.webContents.executeJavaScript(`(() => {
                document.body.innerHTML = ${JSON.stringify(appNode.innerHTML)};
                const map = document.querySelector('.map-view');
                map.style.width = '${width}px';
                const toolbar = map.querySelector('.map-toolbar');
                const bounds = toolbar.getBoundingClientRect();
                const buttons = [...toolbar.querySelectorAll('button')];
                const rects = buttons.map(button => button.getBoundingClientRect());
                const available = toolbar.clientWidth - parseFloat(getComputedStyle(toolbar).paddingLeft) - parseFloat(getComputedStyle(toolbar).paddingRight);
                const readable = buttons.every((button, i) => {
                  const probe = button.cloneNode(true);
                  probe.style.cssText = 'position:fixed; visibility:hidden; width:max-content; max-width:none; flex:none';
                  button.parentElement.append(probe);
                  const preferredWidth = probe.getBoundingClientRect().width;
                  probe.remove();
                  return rects[i].width >= Math.min(preferredWidth, available) - 1;
                });
                return {
                  mapFits: map.scrollWidth <= map.clientWidth + 1,
                  toolbarFits: toolbar.scrollWidth <= toolbar.clientWidth + 1,
                  controlsFit: rects.every(rect => rect.width > 0 && rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1 && rect.bottom <= bounds.bottom + 1),
                  overlap: rects.some((rect, i) => rects.slice(i + 1).some(other => rect.left < other.right - 1 && rect.right > other.left + 1 && rect.top < other.bottom - 1 && rect.bottom > other.top + 1)),
                  readable,
                };
              })()`);
              const label = `${viewport}px viewport / ${width}px map / ${locale} / ${mode} / selection=${selection}: ${JSON.stringify(result)}`;
              assert.ok(result.mapFits && result.toolbarFits && result.controlsFit && !result.overlap && result.readable, `工具栏拥挤或溢出 ${label}`);
              count++;
            }
          }
        }
      }
    } finally {
      win.destroy();
    }
  }
  console.log(`map-toolbar-responsive: ${count} 组窗口/地图宽度/语言/模式/批量操作组合通过`);
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
