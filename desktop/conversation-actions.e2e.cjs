/* 隐藏 Electron 验证真实 DOM 的复制内容、反馈、键盘可达性与 Markdown 文件下载。
 * 剪贴板仅替换系统边界，避免覆盖用户剪贴板；下载使用 Electron 的实际 will-download 路径。 */
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

if (!process.versions.electron) {
  require("node:test")("conversation copy and export in hidden Electron", () => {
    const env = { ...process.env, FOCUS_CONVERSATION_TEST_DIR: fs.mkdtempSync(path.join(os.tmpdir(), "focus-conversation-actions-")) };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 60000, windowsHide: true });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /CONVERSATION_ACTIONS_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_CONVERSATION_TEST_DIR);
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1200, height: 900, webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    } });
    const run = code => win.webContents.executeJavaScript(code, true);
    const until = async expression => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${expression})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${expression}`);
        await new Promise(resolve => setTimeout(resolve, 25));
      }
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until('document.querySelector(\'[data-action="focus-home"]\')');
    await run('document.querySelector(\'[data-action="focus-home"]\').click()');
    await until('document.querySelector(".focus-view")');
    const source = '**回答** "双引号" & <原始标签>\n\n```python\n\t  print("<ok> & done")\n  second = 2  \n```\n\n    indented code\n';
    await run(`window.copiedTexts = []; window.copyDenied = false;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { if (copyDenied) throw new Error('denied'); copiedTexts.push(text); } } });
      window.answerSource = ${JSON.stringify(source)};
      const detail = state.details.get(state.activeTaskId);
      detail.messages = Array.from({ length: 100 }, (_, i) => ({ id: 'copy-history-' + i, role: i % 2 ? 'ai' : 'human', content: '历史正文 ' + i }));
      detail.messages.push({ id: 'copy-answer-final', role: 'ai', content: answerSource });
      renderFocus();`);
    const answerSelector = '[data-message-key="copy-answer-final"] [data-action="copy-answer"]';
    assert.equal(await run(`document.querySelector(${JSON.stringify(answerSelector)}).dataset.copyText`), source);
    await run(`document.querySelector(${JSON.stringify(answerSelector)}).click()`);
    await until('copiedTexts.length === 1');
    assert.equal(await run('copiedTexts[0]'), source);
    assert.equal(await run(`document.querySelector(${JSON.stringify(answerSelector)}).textContent`), "已复制");
    const codeSelector = '[data-message-key="copy-answer-final"] [data-action="copy-code"]';
    await run(`document.querySelector(${JSON.stringify(codeSelector)}).focus()`);
    assert.equal(await run(`document.activeElement.matches('[data-action="copy-code"]')`), true);
    await run(`document.querySelector(${JSON.stringify(codeSelector)}).click()`);
    await until('copiedTexts.length === 2');
    assert.equal(await run('copiedTexts[1]'), '\t  print("<ok> & done")\n  second = 2  \n');
    await run(`copyDenied = true; document.querySelectorAll(${JSON.stringify(codeSelector)})[1].click()`);
    await until(`document.querySelectorAll(${JSON.stringify(codeSelector)})[1].textContent.includes('复制失败')`);
    await until(`!document.querySelectorAll(${JSON.stringify(codeSelector)})[1].disabled`);
    await run(`copyDenied = false; document.querySelectorAll(${JSON.stringify(codeSelector)})[1].click()`);
    await until('copiedTexts.length === 3');
    assert.equal(await run('copiedTexts[2]'), 'indented code\n');

    const downloadPath = path.join(app.getPath("userData"), "conversation.md");
    const downloaded = new Promise((resolve, reject) => {
      win.webContents.session.once("will-download", (_event, item) => {
        item.setSavePath(downloadPath);
        item.once("done", (_event, state) => state === "completed" ? resolve() : reject(new Error(`Download ${state}`)));
      });
    });
    await run(`document.querySelector('.task-workspace-tools details').open = true; document.querySelector('[data-action="export-conversation"]').click()`);
    await downloaded;
    const markdown = fs.readFileSync(downloadPath, "utf8");
    assert.match(markdown, /历史正文 0\n/);
    assert.match(markdown, /历史正文 99\n/);
    assert.ok(markdown.includes(source));
    assert.doesNotMatch(markdown, /复制回答|复制代码|任务工具/);
    await run(`FocusI18n.setLocale('en-US'); renderFocus()`);
    assert.equal(await run(`document.querySelector(${JSON.stringify(answerSelector)}).textContent`), "Copy answer");
    assert.equal(await run(`document.querySelector(${JSON.stringify(codeSelector)}).textContent`), "Copy code");
    await win.setSize(900, 760);
    assert.equal(await run('document.documentElement.scrollWidth <= innerWidth'), true);
    console.log("CONVERSATION_ACTIONS_PASS", app.getPath("userData"));
    win.destroy();
    app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
