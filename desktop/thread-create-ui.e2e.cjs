/*
 * 本文件对外提供新线程的原生 Electron 提交回归。
 * 输入为真实 index.html 和受控本地 API；输出为点击、Enter、并发提交只创建一次及失败保留输入的断言。
 * 具体工作流为原生键盘触发表单，响应屏障阻塞重复提交，服务失败后核对对话框和输入，再实际重试。
 * 示例：electron desktop/thread-create-ui.e2e.cjs。
 */
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { app, BrowserWindow } = require("electron");

app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-thread-create-")));
app.disableHardwareAcceleration();

async function evaluate(window, code) {
  return window.webContents.executeJavaScript(code);
}

async function waitFor(window, predicate) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await evaluate(window, predicate)) return;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw new Error(`UI condition timed out: ${predicate}`);
}

async function openForm(window, title) {
  await evaluate(window, `document.querySelector('[data-action="new-task"]').click();
    document.querySelector('#workspacePath').value='C:/workspace';
    document.querySelector('#threadTitle').value=${JSON.stringify(title)};
    document.querySelector('#threadTitle').focus();`);
  await waitFor(window, "document.querySelector('#taskDialog').open");
}

async function run() {
  const window = new BrowserWindow({ show: true, webPreferences: {
    preload: path.join(__dirname, "context-ui-test-preload.cjs"), contextIsolation: false, nodeIntegration: false,
  } });
  await window.loadFile(path.join(__dirname, "index.html"));
  await waitFor(window, "!!document.querySelector('[data-action=\"new-task\"]')");
  await openForm(window, "clicked");
  await evaluate(window, "document.querySelector('#taskForm button[type=submit]').click()");
  await waitFor(window, "!document.querySelector('#taskDialog').open && state.activeTaskId==='created-1'");
  await openForm(window, "keyboard");
  window.focus();
  window.webContents.focus();
  window.webContents.sendInputEvent({ type: "keyDown", keyCode: "Enter" });
  window.webContents.sendInputEvent({ type: "char", keyCode: "Enter" });
  window.webContents.sendInputEvent({ type: "keyUp", keyCode: "Enter" });
  await waitFor(window, "!document.querySelector('#taskDialog').open && state.activeTaskId==='created-2'");
  await openForm(window, "duplicate");
  await evaluate(window, "threadUiHarness.hold=true; document.querySelector('#taskForm').requestSubmit(); document.querySelector('#taskForm').requestSubmit()");
  await waitFor(window, "threadUiHarness.submissions.length===3 && !!threadUiHarness.release");
  await evaluate(window, "if(!document.querySelector('#taskForm button[type=submit]').disabled) throw Error('submit not disabled'); threadUiHarness.hold=false; threadUiHarness.release()");
  await waitFor(window, "!document.querySelector('#taskDialog').open && state.activeTaskId==='created-3'");
  await openForm(window, "retained-on-error");
  await evaluate(window, "threadUiHarness.fail=true; document.querySelector('#taskForm button[type=submit]').click()");
  await waitFor(window, "!state.creatingTask && threadUiHarness.submissions.length===4");
  await evaluate(window, `if(!document.querySelector('#taskDialog').open || document.querySelector('#workspacePath').value!=='C:/workspace' || document.querySelector('#threadTitle').value!=='retained-on-error') throw Error('failed submission lost dialog inputs');
    threadUiHarness.fail=false; document.querySelector('#taskForm button[type=submit]').click();`);
  await waitFor(window, "!document.querySelector('#taskDialog').open && state.activeTaskId==='created-5'");
  await evaluate(window, "if(threadUiHarness.submissions.map(x=>x.title).join('|')!=='clicked|keyboard|duplicate|retained-on-error|retained-on-error') throw Error('unexpected duplicate request')");
  console.log("PASS native Electron thread click, Enter, duplicate and retained failure retry");
  window.destroy();
}

app.whenReady().then(run).then(() => app.exit(0)).catch(error => { console.error(error); app.exit(1); });
