/** 普通会话生产页面的补充/排队/撤回编辑/删除与原请求恢复验收。 */
const assert = require("node:assert/strict"), path = require("node:path"), fs = require("node:fs"), os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 90000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-conversation-inputs-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 1000, webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false } });
    const errors = [];
    win.webContents.on("console-message", event => { if (event.message?.includes("Uncaught")) errors.push(event.message); });
    const run = code => win.webContents.executeJavaScript(code, true);
    const until = async expression => {
      const deadline = Date.now() + 10000;
      while (!await run(`Boolean(${expression})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${expression}\n${errors.join("\n")}\n${await run("document.body.innerText.slice(-2500)")}`);
        await new Promise(resolve => setTimeout(resolve, 30));
      }
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until("state.tasks.length > 0");
    await run("switchTask('child')");
    await until("state.details.has('child')");
    await run(`
      window.inputFixture = { rows: [], posts: [], fail: false, failed: null };
      const baseInputFetch = window.fetch;
      window.fetch = async (input, options = {}) => {
        const pathname = new URL(String(input), 'http://focus.test').pathname;
        const match = pathname.match(/\\/main\\/inputs(?:\\/([^/]+))?$/);
        if (!match) return baseInputFetch(input, options);
        const json = (value, status=200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
        if (options.method === 'POST') {
          const body = JSON.parse(options.body), key = new Headers(options.headers).get('Idempotency-Key');
          inputFixture.posts.push({body, key});
          let row = inputFixture.rows.find(row => row.input_id === key);
          if (!row) { row = {...body, input_id:key, task_id:state.activeTaskId, status:body.mode==='queue'?'queued':'accepted', run_id:null}; inputFixture.rows.push(row); }
          if (inputFixture.fail) { inputFixture.fail=false; inputFixture.failed={body, key}; return json({detail:'response lost'}, 500); }
          return json(row);
        }
        if (options.method === 'DELETE') {
          const row = inputFixture.rows.find(row => row.input_id === match[1]); row.status='withdrawn'; return json(row);
        }
        return json(inputFixture.rows.filter(row=>row.status!=='withdrawn'));
      };
      state.view='focus'; state.activeTaskId='child';
      state.details.get('child').active_run={run_id:'current',task_id:'child',kind:'main',status:'running'};
      renderFocus();
    `);
    assert.equal(await run("document.querySelector('[data-action=send-main]').textContent"), "补充当前轮");
    await run("document.querySelector('#mainInput').value='先不要改数据库';document.querySelector('[data-action=send-main]').click()");
    await until("inputFixture.posts.length===1 && document.querySelector('#mainInput').value===''");
    assert.deepEqual(await run("inputFixture.posts[0].body.mode"), "steer");
    assert.equal(await run("inputFixture.posts[0].body.target_run_id"), "current");
    await until("document.querySelector('#conversationInputs').textContent.includes('已受理')");
    await run("inputFixture.rows[0].status='delivered'");
    await until("document.querySelector('#conversationInputs').textContent.includes('已进入当前轮上下文')");
    await run("document.querySelector('#mainInput').value='结束后补一组测试';document.querySelector('[data-action=queue-main]').click()");
    await until("inputFixture.posts.length===2 && document.querySelector('[data-action=edit-queued-main]')");
    assert.equal(await run("inputFixture.posts[1].body.mode"), "queue");
    await run("document.querySelector('[data-action=edit-queued-main]').click()");
    await until("document.querySelector('#mainInput').value==='结束后补一组测试'");
    assert.equal(await run("inputFixture.rows[1].status"), "withdrawn");
    await run("document.querySelector('#mainInput').value='结束后补两组测试';document.querySelector('[data-action=queue-main]').click()");
    await until("inputFixture.posts.length===3 && document.querySelector('[data-action=delete-queued-main]')");
    assert.equal(await run("inputFixture.posts[2].body.run.message"), "结束后补两组测试");
    await run("document.querySelector('[data-action=delete-queued-main]').click()");
    await until("!document.querySelector('[data-action=delete-queued-main]')");
    await run("inputFixture.fail=true; document.querySelector('#mainInput').value='幂等重试';document.querySelector('[data-action=send-main]').click()");
    await until("taskRunOperations.recovery('child') && !taskRunOperations.get('child').submission");
    await run("document.querySelector('[data-action=retry-main]').click()");
    await until("inputFixture.posts.length===5 && !taskRunOperations.recovery('child')");
    const posts = await run("inputFixture.posts.slice(-2)");
    assert.deepEqual(posts[0], posts[1]);
    assert.equal(await run("taskRunOperations.get('child').active_run"), null);
    assert.deepEqual(errors, []);
    console.log("PASS ordinary conversation: steer receipt, delivery refresh, queue, withdraw/edit, delete, immutable retry, Run identity");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
