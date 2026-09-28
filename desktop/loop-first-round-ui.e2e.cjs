/*
 * 本文件对外提供同一持久 Run 的真实 Electron 首轮页面验收入口。
 * 输入为测试后端 URL、Loop/Context/Run 身份和截图路径；输出为真实 HTTP/SSE 驱动的活动可见性、首次故障恢复、固定 revision 保持与截图证据。
 * 具体工作流为加载实际桌面应用，通过 preload 请求隔离库上的生产 Loop API，等待首个 Live GET 暂时失败后重连并显示正在执行的 Run 活动，再核对会话与输入不被更新覆盖。示例：`electron loop-first-round-ui.e2e.cjs evidence/first-round.png`。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

app.setPath("userData", path.join(os.tmpdir(), `focus-first-round-ui-${process.pid}`));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

async function waitFor(win, expression, label) {
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 250; count += 1) {
      if (${expression}) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error(${JSON.stringify(label)}));
  })`);
}

async function run() {
  const contextId = process.env.FOCUS_LOOP_E2E_CONTEXT_ID;
  const runId = process.env.FOCUS_LOOP_E2E_RUN_ID;
  if (!process.env.FOCUS_LOOP_E2E_SERVER || !contextId || !runId) throw new Error("首轮集成验收缺少真实后端或 Run 身份");
  const win = new BrowserWindow({
    show: false,
    width: 1440,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, "loop-live-integration-preload.cjs"),
      contextIsolation: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });
  win.webContents.on("preload-error", (_event, preload, error) => console.error(`preload-error ${preload}: ${error.stack || error}`));
  await win.loadFile(path.join(__dirname, "index.html"));
  await waitFor(win, "typeof openLoopView === 'function' && Boolean(state.activeTaskId)", "Desktop 初始化超时");
  await win.webContents.executeJavaScript(`(async () => {
    const contextId = ${JSON.stringify(contextId)};
    state.tasks = state.tasks.map(task => task.task_id === 'root' ? { ...task, task_id: contextId, thread_id: 'thread-' + contextId } : task);
    state.activeTaskId = contextId;
    await hydrateActive(contextId);
    await openLoopView();
  })()`);
  await waitFor(win, "loopLiveStore.get().connection.status === 'live'", "首轮 Live SSE 未连接");
  const unhandled = await win.webContents.executeJavaScript("document.querySelector('#globalStatus')?.textContent || ''");
  if (unhandled.includes("未处理 Promise")) throw new Error(`首次 Live GET 失败产生未处理 Promise：${unhandled}`);
  const resultPath = process.env.FOCUS_LOOP_E2E_RESULT_FILE;
  const afterSequence = await win.webContents.executeJavaScript("loopLiveStore.get().projection.last_sequence");
  fs.writeFileSync(resultPath, JSON.stringify({ ready: true, run_id: runId, after_sequence: afterSequence }));
  try {
    await waitFor(win, `Boolean(loopLiveStore.get().projection.runs[${JSON.stringify(runId)}]) && document.querySelector('.context-execution-activity')?.textContent.includes('pytest 正在执行')`, "同一 Run 的实时活动未到达桌面");
  } catch (error) {
    const observed = await win.webContents.executeJavaScript(`(() => ({
      connection: loopLiveStore.get().connection,
      sequence: loopLiveStore.get().projection.last_sequence,
      run: loopLiveStore.get().projection.runs[${JSON.stringify(runId)}],
      current: document.querySelector('.context-execution-activity')?.textContent,
      rail: document.querySelector('.patrol-activity-rail')?.textContent,
      text: document.querySelector('.loop-dashboard')?.textContent?.slice(0, 1500),
    }))()`);
    throw new Error(`${error.message}: ${JSON.stringify(observed)}`);
  }
  const visible = await win.webContents.executeJavaScript(`(() => {
    const transcript = document.querySelector('[data-loop-transcript]');
    const composer = document.querySelector('#loopInterventionForm textarea');
    composer.value = '尚未发送的用户输入';
    transcript.scrollTop = 23;
    return {
      runId: ${JSON.stringify(runId)},
      rail: document.querySelector('.patrol-activity-rail')?.textContent || '',
      current: document.querySelector('.context-execution-activity')?.textContent || '',
      history: transcript?.textContent || '',
      draft: composer.value,
      connection: loopLiveStore.get().connection.status,
      sequence: loopLiveStore.get().projection.last_sequence,
    };
  })()`);
  if (!visible.current.includes("pytest 正在执行") || !visible.history.includes("历史 revision 已提交") || visible.draft !== "尚未发送的用户输入" || visible.connection !== "live") {
    throw new Error(`同一 Run 页面验收失败: ${JSON.stringify(visible)}`);
  }
  const capturedView = await win.webContents.executeJavaScript(`(async () => {
    state.view = 'loop';
    render();
    document.querySelector('.loop-dashboard')?.scrollIntoView({ block: 'start' });
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return {
      view: state.view,
      current: document.querySelector('.context-execution-activity')?.textContent || '',
      dashboard: Boolean(document.querySelector('.loop-dashboard')),
      liveSummary: loopLiveStore.get().projection.runs[${JSON.stringify(runId)}]?.state?.last_activity_summary,
      loopSummary: loopStore.get().live.runs[${JSON.stringify(runId)}]?.state?.last_activity_summary,
    };
  })()`);
  if (capturedView.view !== "loop" || !capturedView.dashboard || !capturedView.current.includes("pytest 正在执行")) {
    throw new Error(`截图未停留在首轮 Loop: ${JSON.stringify(capturedView)}`);
  }
  const target = path.resolve(process.argv[2] || path.join(os.tmpdir(), "focus-loop-first-round.png"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  await win.webContents.executeJavaScript("document.body.getBoundingClientRect().width");
  await new Promise(resolve => setTimeout(resolve, 400));
  await win.webContents.capturePage();
  await new Promise(resolve => setTimeout(resolve, 150));
  fs.writeFileSync(target, (await win.webContents.capturePage()).toPNG());
  const afterCapture = await win.webContents.executeJavaScript(`({
    current: document.querySelector('.context-execution-activity')?.textContent || '',
    liveSummary: loopLiveStore.get().projection.runs[${JSON.stringify(runId)}]?.state?.last_activity_summary,
    loopSummary: loopStore.get().live.runs[${JSON.stringify(runId)}]?.state?.last_activity_summary,
  })`);
  if (!afterCapture.current.includes("pytest 正在执行")) throw new Error(`截图期间活动被旧状态覆盖: ${JSON.stringify({ capturedView, afterCapture })}`);
  fs.writeFileSync(resultPath, JSON.stringify({ screenshot: target, run_id: runId, context_id: contextId, sse_sequence: visible.sequence, first_round_without_patrol: true }));
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  if (process.env.FOCUS_LOOP_E2E_RESULT_FILE) fs.writeFileSync(process.env.FOCUS_LOOP_E2E_RESULT_FILE, JSON.stringify({ error: error.stack || String(error) }));
  console.error(error.stack || error);
  app.exit(1);
});
