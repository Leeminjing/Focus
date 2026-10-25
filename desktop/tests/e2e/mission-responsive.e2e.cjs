/* 使用生产 Mission 模板/CSS 验证检查项按表单宽度切换布局。
 * 运行：electron tests/e2e/mission-responsive.e2e.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow } = require("electron");

const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "focus-mission-responsive-"));
app.setPath("userData", path.join(fixtureDir, "user-data"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.on("window-all-closed", () => {});

async function run() {
  const renderer = path.resolve(__dirname, "../../src/renderer");
  const html = fs.readFileSync(path.join(renderer, "index.html"), "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace("<head>", `<head><base href="${pathToFileURL(path.join(renderer, "index.html")).href}">`);
  const fixturePath = path.join(fixtureDir, "mission.html");
  fs.writeFileSync(fixturePath, html);
  let count = 0;
  for (const viewport of [1440, 900]) {
    const win = new BrowserWindow({ width: viewport, height: 1000, useContentSize: true, show: false, webPreferences: { backgroundThrottling: false } });
    try {
      await win.loadFile(fixturePath);
      for (const source of ["shared/i18n/i18n-catalog.js", "shared/i18n/i18n.js", "features/loop/loop-mission-editor.js"]) {
        await win.webContents.executeJavaScript(fs.readFileSync(path.join(renderer, source), "utf8"));
      }
      for (const locale of ["zh-CN", "en-US"]) {
        for (const width of [760, 641, 640, 520, 360]) {
          const result = await win.webContents.executeJavaScript(`(() => {
            FocusI18n.setLocale('${locale}');
            document.body.innerHTML = '<form class="loop-start-form" style="width:${width}px; margin:16px">' + FocusLoopMissionEditor.render({}) + '</form>';
            const form = document.querySelector('form');
            const row = form.querySelector('.loop-mission-check');
            const bounds = row.getBoundingClientRect();
            return {
              columns: getComputedStyle(row).gridTemplateColumns.split(/\\s+/).length,
              formWidth: form.getBoundingClientRect().width,
              rowWidth: bounds.width,
              controlsFit: [...row.querySelectorAll('input, select, button')].every(node => {
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1;
              }),
              sectionFits: [...form.querySelectorAll('.loop-mission-section')].every(node => node.scrollWidth <= node.clientWidth + 1),
            };
          })()`);
          const label = `${viewport}px viewport / ${width}px form / ${locale}: ${JSON.stringify(result)}`;
          assert.equal(result.formWidth, width, label);
          assert.equal(result.columns, width <= 640 ? 1 : 4, `检查项未按表单宽度切换 ${label}`);
          assert.ok(result.rowWidth <= width && result.controlsFit && result.sectionFits, `表单或控件横向溢出 ${label}`);
          count++;
        }
      }
    } finally {
      win.destroy();
    }
  }
  console.log(`mission-responsive: ${count} 组窗口/表单宽度/语言组合通过`);
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
