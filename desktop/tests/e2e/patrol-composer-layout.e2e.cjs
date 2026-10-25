/* 使用生产 Patrol 模板和 CSS 验证状态提示布局和输入记录的视口边界。
 * 运行：electron tests/e2e/patrol-composer-layout.e2e.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow } = require("electron");
const view = require("../../src/renderer/features/patrol/workspace/workspace-patrol-view.js");

const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-composer-layout-"));
app.setPath("userData", path.join(fixtureDir, "user-data"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.on("window-all-closed", () => {});

async function run() {
  const indexPath = path.resolve(__dirname, "../../src/renderer/index.html");
  const html = fs.readFileSync(indexPath, "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace("<head>", `<head><base href="${pathToFileURL(indexPath).href}">`);
  const fixturePath = path.join(fixtureDir, "patrol.html");
  fs.writeFileSync(fixturePath, html);
  const skeleton = view.skeleton({ workspace_id: "layout", display_name: "布局回归" });
  let count = 0;
  for (const width of [1440, 1000, 600]) {
    const win = new BrowserWindow({ width, height: 900, useContentSize: true, show: false, webPreferences: { backgroundThrottling: false } });
    try {
      await win.loadFile(fixturePath);
      for (const quiet of [false, true]) {
        for (const deliveryVisible of [false, true]) {
          const result = await win.webContents.executeJavaScript(`(() => {
            document.querySelector('#app').innerHTML = ${JSON.stringify(skeleton)};
            const patrol = document.querySelector('.workspace-patrol');
            patrol.classList.toggle('is-quiet', ${quiet});
            const details = patrol.querySelector('[data-patrol-details-content]');
            details.hidden = ${quiet};
            details.inert = ${quiet};
            const composer = patrol.querySelector('[data-patrol-composer]');
            const status = patrol.querySelector('[data-patrol-state]');
            const delivery = patrol.querySelector('[data-patrol-delivery]');
            delivery.hidden = ${!deliveryVisible};
            delivery.textContent = '本次工作交付：结果已提交，可以继续提出调整。';
            const measure = () => ({
              composer: composer.getBoundingClientRect().toJSON(),
              status: status.getBoundingClientRect().toJSON(),
              delivery: delivery.getBoundingClientRect().toJSON(),
            });
            status.textContent = '正在读取工作状态';
            const short = measure();
            status.textContent = '工作状态正在重新连接，请稍后重试并检查当前工作区的执行进度。'.repeat(12);
            const long = measure();
            status.textContent = 'ReconnectWorkspaceStatusWithoutSpaces'.repeat(20);
            const unbroken = measure();
            return { short, long, unbroken };
          })()`);
          const label = `${width}px quiet=${quiet} delivery=${deliveryVisible}`;
          for (const key of ["long", "unbroken"]) {
            const measurement = result[key];
            assert.ok(measurement.status.height > result.short.status.height, `未触发多行状态 ${label} ${key}`);
            const statusGrowth = measurement.status.height - result.short.status.height;
            const composerGrowth = measurement.composer.height - result.short.composer.height;
            assert.ok(composerGrowth >= statusGrowth - 1, `状态未撑开输入框 ${label} ${key}`);
            assert.ok(measurement.status.bottom <= measurement.composer.bottom + 1, `状态超出输入框 ${label} ${key}`);
            assert.ok(measurement.status.right <= measurement.composer.right + 1, `长状态横向溢出 ${label} ${key}`);
            if (deliveryVisible) assert.ok(measurement.delivery.top >= measurement.status.bottom - 1, `状态覆盖交付卡片 ${label} ${key}`);
          }
          count++;
        }
      }
    } finally {
      win.destroy();
    }
  }
  console.log(`patrol-composer-layout: ${count} 组宽度/视图/交付卡片组合，多行和无空格状态通过`);
  let historyCount = 0;
  for (const [width, height] of [[1920, 500], [1440, 400], [900, 400], [1440, 942]]) {
    const win = new BrowserWindow({ width, height, useContentSize: true, show: false, webPreferences: { backgroundThrottling: false } });
    try {
      await win.loadFile(fixturePath);
      for (const quiet of [false, true]) {
        const result = await win.webContents.executeJavaScript(`(async () => {
          const app = document.querySelector('#app');
          app.innerHTML = ${JSON.stringify(skeleton)};
          const patrol = app.querySelector('.workspace-patrol');
          patrol.classList.toggle('is-quiet', ${quiet});
          const details = patrol.querySelector('[data-patrol-details-content]');
          details.hidden = ${quiet};
          details.inert = ${quiet};
          const panel = patrol.querySelector('[data-patrol-input-history]');
          panel.hidden = false;
          panel.inert = false;
          patrol.querySelector('[data-patrol-history]').innerHTML = Array.from({length: 30}, (_, i) => '<article><pre>输入记录 ' + i + '：保留阅读位置，继续编辑输入框。</pre></article>').join('');
          const measure = () => ({ rect: panel.getBoundingClientRect().toJSON(), clientHeight: panel.clientHeight, scrollHeight: panel.scrollHeight });
          const initial = measure();
          panel.scrollTop = panel.scrollHeight;
          const scrollTop = panel.scrollTop;
          app.scrollTop = app.scrollHeight;
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return { initial, scrolled: measure(), scrollTop, viewportHeight: innerHeight };
        })()`);
        const label = `${width}x${height} quiet=${quiet}: ${JSON.stringify(result)}`;
        for (const measurement of [result.initial, result.scrolled]) {
          assert.ok(measurement.rect.top >= 15, `输入记录超出视口上缘 ${label}`);
          assert.ok(measurement.rect.bottom <= result.viewportHeight - 15, `输入记录超出视口下缘 ${label}`);
          assert.ok(measurement.rect.height <= Math.min(360, result.viewportHeight * .5) + 1, `输入记录高度仍受输入框宽度放大 ${label}`);
          assert.ok(measurement.clientHeight > 0 && measurement.scrollHeight > measurement.clientHeight, `输入记录未保留内部滚动 ${label}`);
        }
        assert.ok(result.scrollTop > 0, `输入记录无法滚动 ${label}`);
        historyCount++;
      }
    } finally {
      win.destroy();
    }
  }
  console.log(`patrol-history-layout: ${historyCount} 组宽高/视图组合，弹层边界和内部滚动通过`);
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
