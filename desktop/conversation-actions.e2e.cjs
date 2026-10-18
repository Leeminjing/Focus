/* 隐藏 Electron 验证真实 DOM 的复制内容、反馈、键盘可达性与 Markdown 文件下载。
 * 剪贴板仅替换系统边界，避免覆盖用户剪贴板；下载使用 Electron 的实际 will-download 路径。 */
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

if (!process.versions.electron) {
  require("node:test")("conversation copy, history retry and export in hidden Electron", () => {
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
    win.webContents.on("console-message", event => { if (event.level === "error") console.error(event.message); });
    const until = async expression => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${expression})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${expression}; ${await run("JSON.stringify({ error: document.querySelector('#messageRetryError')?.textContent, status: document.querySelector('#globalStatus')?.textContent, task: state.activeTaskId, view: state.view, derives: window.retryHarness?.derives.length })")}`);
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

    // Exercise history actions through real buttons/dialogs and the production send
    // path. Only API/model boundaries are replaced; the original task remains intact.
    await run(`(async () => { FocusI18n.setLocale('zh-CN');
      window.retryHistory = [
        { id: 'history-q1', role: 'human', content: '原始提问' },
        { id: 'history-a1', role: 'ai', content: '**原始回答**' },
        { id: 'history-q2', role: 'human', content: '后续提问' },
        { id: 'history-a2', role: 'ai', content: '后续回答' }
      ];
      window.retryHarness = { derives: [], runs: [], ui: {}, modes: {} };
      window.previousFetch = window.fetch;
      window.fetch = async (url, options = {}) => {
        const path = new URL(String(url), 'http://focus.test').pathname;
        const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
        if (path === '/desktop/api/contexts/root/snapshot') return json({ checkpoint_id: 'history-checkpoint', messages: retryHistory });
        if (path === '/desktop/api/contexts/derive') {
          retryHarness.derives.push(JSON.parse(options.body));
          return json({ context_id: 'retry-' + retryHarness.derives.length, projection_status: 'valid' });
        }
        if (path === '/desktop/api/tasks') return json([...state.tasks.filter(task => !task.task_id.startsWith('retry-')), ...retryHarness.derives.map((body, i) => ({ task_id: 'retry-' + (i + 1), title: body.title, workspace_id: 'workspace', workspace_path: 'C:/workspace', active_run: null }))]);
        if (path.startsWith('/desktop/api/tasks/retry-')) {
          const id = path.split('/')[4];
          if (path.endsWith('/access-mode')) { retryHarness.modes[id] = JSON.parse(options.body).access_mode; return json({}); }
          if (path.endsWith('/ui-state')) { retryHarness.ui[id] = JSON.parse(options.body); return json({}); }
          if (path.endsWith('/main/runs')) { retryHarness.runs.push({ id, body: JSON.parse(options.body), key: options.headers.get('Idempotency-Key') }); return json({ run_id: 'run-' + id, message_id: 'question-' + id, kind: 'main', task_id: id, status: 'pending' }); }
          if (path.split('/').length === 5) return json({ task_id: id, messages: retryHarness.derives[Number(id.split('-')[1]) - 1].messages, active_run: null, ui_state: { input: '', access_mode: retryHarness.modes[id] || 'read-only' } });
          if (['/materials', '/material-history', '/material-groups', '/agents'].some(suffix => path.endsWith(suffix))) return json([]);
          if (path.endsWith('/skills')) return json({ skills: [] });
        }
        return previousFetch(url, options);
      };
      listenToRun = () => {};
      await switchTask('root');
      state.details.get('root').messages = structuredClone(retryHistory);
      composerDraft.claim('root', '尚未发送的草稿'); renderFocus(); })()`);
    const oldQuestion = '[data-message-key="history-q1"]';
    const oldAnswer = '[data-message-key="history-a1"]';
    await run(`document.querySelector('${oldAnswer} [data-action="quote-history-message"]').click()`);
    assert.equal(await run(`document.querySelector('#mainInput').value`), '尚未发送的草稿\n\n> **原始回答**\n\n');
    assert.equal(await run('retryHarness.runs.length'), 0);
    await run(`document.querySelector('${oldQuestion} [data-action="edit-history-message"]').click()`);
    await until('document.querySelector("#messageRetryDialog")?.open');
    assert.equal(await run('document.activeElement.id'), 'messageRetryInput');
    assert.equal(await run('document.querySelector("#messageRetryInput").value'), '原始提问');
    await until('Number(getComputedStyle(document.querySelector("#messageRetryDialog")).opacity) >= 0.99');
    assert.equal(await run(`(() => { const button = document.querySelector('[data-action="cancel-message-retry"]'); const box = button.getBoundingClientRect(); return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) === button; })()`), true);
    fs.writeFileSync(path.join(app.getPath('userData'), 'history-edit.png'), (await win.webContents.capturePage()).toPNG());
    await run(`document.querySelector('[data-action="cancel-message-retry"]').click()`);
    assert.equal(await run('retryHarness.derives.length'), 0);
    assert.equal(await run(`document.activeElement.matches('${oldQuestion} [data-action="edit-history-message"]')`), true);
    await run(`document.querySelector('${oldQuestion} [data-action="edit-history-message"]').click()`);
    await until('document.querySelector("#messageRetryDialog")?.open');
    await run(`document.querySelector('#messageRetryInput').value = '编辑后的提问';
      document.querySelector('[data-action="submit-message-retry"]').click();
      document.querySelector('[data-action="submit-message-retry"]').click();`);
    await until('retryHarness.runs.length === 1');
    assert.equal(await run('retryHarness.derives.length'), 1);
    assert.deepEqual(await run('retryHarness.derives[0].messages'), []);
    assert.equal(await run('retryHarness.runs[0].body.message'), '编辑后的提问');
    assert.equal(await run('retryHarness.runs[0].id'), 'retry-1');
    assert.equal(await run(`state.details.get('root').messages.length`), 4);
    assert.equal(await run(`composerDraft.value('root')`), '尚未发送的草稿\n\n> **原始回答**\n\n');
    assert.equal(await run('document.querySelector("#messageRetryDialog")'), null);
    await run(`(async () => { await switchTask('root'); state.details.get('root').messages = structuredClone(retryHistory); renderFocus();
      document.querySelector('${oldAnswer} [data-action="retry-history-message"]').click(); })()`);
    await until('retryHarness.runs.length === 2');
    assert.equal(await run('retryHarness.runs[1].body.message'), '原始提问');
    assert.equal(await run('retryHarness.runs[1].id'), 'retry-2');
    assert.deepEqual(await run('retryHarness.derives[1].messages'), []);
    await win.setSize(760, 760);
    assert.equal(await run('document.documentElement.scrollWidth <= innerWidth'), true);
    console.log("CONVERSATION_ACTIONS_PASS", app.getPath("userData"));
    win.destroy();
    app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
