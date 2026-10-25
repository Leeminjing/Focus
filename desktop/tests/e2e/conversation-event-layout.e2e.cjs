/* 验证生产事件详情在宽窗口窄会话栏与窄窗口内展开、换行和收起时的宽度。
 * 示例：node desktop/tests/e2e/conversation-event-layout.e2e.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 30000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-event-layout-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 900, webPreferences: { offscreen: true, backgroundThrottling: false } });
    const events = require("../../src/renderer/features/conversation/conversation-events.js");
    const source = file => fs.readFileSync(path.join(__dirname, "../../src/renderer", file), "utf8");
    const styles = ["shared/styles/tokens.css", "shared/styles/base.css", "features/conversation/conversation-events.css"].map(source).join("\n");
    const fixtures = [
      { type: "reasoning", content: "思考过程与最新结论。".repeat(100) },
      { type: "tool", status: "success", call: { id: "read", name: "read_file", args: { path: "docs/" + "long-path/".repeat(60) } }, result: { content: "unbroken_output_".repeat(200) } },
      { type: "tool", status: "error", call: { id: "error", name: "shell", args: { command: "exit 1" } }, result: { content: "失败输出。".repeat(100) } },
    ];
    const html = fixtures.map(event => events.renderEvent(event).replace('data-detail-lazy="1"></div>', `>${events.renderEventDetail(event)}</div>`)).join("");
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>${styles}body{margin:0;padding:24px;overflow:auto}#conversation{width:360px}</style><main id="conversation"><section class="conversation-event-sequence">${html}</section></main>`));
    const run = code => win.webContents.executeJavaScript(code);
    for (const viewport of [1440, 800]) {
      win.setContentSize(viewport, 900);
      for (const width of [320, 360, 400, 740]) {
        await run(`document.querySelector('#conversation').style.width='${width}px'`);
        for (let index = 0; index < fixtures.length; index++) {
          const result = await run(`(async () => {
            const host=document.querySelector('#conversation');
            const row=host.querySelectorAll('details')[${index}];
            const closedWidth=row.getBoundingClientRect().width;
            row.querySelector('summary').click();
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            const detail=row.querySelector('.conversation-event-detail');
            const rect=detail.getBoundingClientRect(), bounds=host.getBoundingClientRect();
            const result={open:row.open,hostWidth:host.clientWidth,scrollWidth:host.scrollWidth,
              detailWidth:rect.width,left:rect.left-bounds.left,right:rect.right-bounds.left,
              textFits:[...detail.querySelectorAll('pre')].every(pre=>pre.scrollWidth<=pre.clientWidth+1)};
            row.querySelector('summary').click();
            result.closedWidthStable=Math.abs(row.getBoundingClientRect().width-closedWidth)<1;
            return result;
          })()`);
          const label = JSON.stringify({ viewport, width, index, ...result });
          assert.ok(result.open && result.detailWidth > 200, label);
          assert.ok(result.right <= width + 1 && result.left >= 0, label);
          assert.ok(result.scrollWidth <= width && result.textFits && result.closedWidthStable, label);
          assert.ok(result.detailWidth <= 720, label);
        }
      }
    }
    console.log("Conversation event layout: PASS (1440/800px windows; 320/360/400/740px columns; reasoning/tool/error)");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
