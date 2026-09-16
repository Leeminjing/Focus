/*
 * 本文件对外提供 Context-governed Agent Loop 的真实 Electron 长流程回归。
 * 输入为确定性 Loop API、真实 index.html/app.js 与用户表单动作；输出为启动离开、恢复重连、
 * 多 Lane、委托来源、用户接管、waiting-user、完成路径和未采用分支的界面断言。
 * 具体工作流为在隐藏 BrowserWindow 中执行完整交互并检查请求与 DOM；示例：`npx electron agent-loop-ui.e2e.cjs`。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-agent-loop-ui-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
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
  const win = new BrowserWindow({
    show: false,
    width: 1440,
    height: 960,
    webPreferences: {
      preload: path.join(__dirname, "agent-loop-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });
  win.webContents.on("console-message", (_event, _level, message) => console.log(`renderer: ${message}`));
  win.webContents.on("preload-error", (_event, preloadPath, error) => console.error(`preload-error ${preloadPath}: ${error.stack || error}`));
  await win.loadFile(path.join(__dirname, "index.html"));
  await waitFor(win, "typeof openLoopView === 'function' && Boolean(state.activeTaskId)", "Desktop 初始化超时");

  const started = await win.webContents.executeJavaScript(`(async () => {
    state.activeTaskId = 'root';
    await hydrateActive('root');
    await openLoopView();
    const form = document.querySelector('#agentLoopStartForm');
    form.elements.goal.value = '交付 Context-governed Agent Loop';
    form.elements.taskContract.value = '保持单 Patrol 权威并通过完整回归';
    form.elements.criteria.value = '多 Context 可并行推进\\n完成必须独立验证';
    form.elements.maxRounds.value = '80';
    form.elements.maxModelCalls.value = '300';
    form.elements.maxLanes.value = '12';
    form.elements.maxContexts.value = '24';
    form.elements.maxProviders.value = '5';
    form.elements.maxConcurrentRuns.value = '6';
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    for (let count = 0; count < 150 && !document.querySelector('.loop-dashboard')?.textContent.includes('继续实现'); count += 1) await new Promise(next => setTimeout(next, 20));
    return { body: window.__agentLoopTest.startBody, text: document.querySelector('.loop-dashboard')?.textContent || '', stored: localStorage.getItem('focus-agent-loop:root') };
  })()`);
  if (!started.stored || started.body?.initial_run_id !== "run-initial" || started.body?.budgets?.max_contexts !== 24 || started.body?.budgets?.max_providers !== 5 || started.body?.budgets?.max_model_calls !== 300) throw new Error(`启动或预算契约失败: ${JSON.stringify(started)}`);
  for (const text of ["round-12", "observing", "继续实现", "测试与故障分析", "架构审查", "需求偏航检查", "Patrol 依据授权生成", "暂停修改代码", "Input 8192", "Output 2048", "Retries 2", "Contexts 5 / 24", "Providers 2 / 5", "撤销 Patrol 授权"]) if (!started.text.includes(text)) throw new Error(`Loop 控制台缺少 ${text}`);

  const resumed = await win.webContents.executeJavaScript(`(async () => {
    state.view = 'focus'; render();
    await openLoopView();
    for (let count = 0; count < 150 && !document.querySelector('.loop-dashboard'); count += 1) await new Promise(next => setTimeout(next, 20));
    return { loopId: state.loop.loopId, cursor: loopStore.get().cursor, text: document.querySelector('.loop-dashboard')?.textContent || '' };
  })()`);
  if (!resumed.loopId || resumed.cursor !== 1 || !resumed.text.includes("已放弃探索")) throw new Error(`离开后恢复或事件重连失败: ${JSON.stringify(resumed)}`);

  const adjusted = await win.webContents.executeJavaScript(`(async () => {
    const form = document.querySelector('#agentLoopBudgetForm');
    form.elements.maxOutputTokens.value = '700000';
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    for (let count = 0; count < 150 && window.__agentLoopTest.grantMutations.length === 0; count += 1) await new Promise(next => setTimeout(next, 20));
    return { mutations: window.__agentLoopTest.grantMutations, revision: loopStore.get().snapshot.authority_revision };
  })()`);
  if (adjusted.mutations[0]?.command !== "adjust_budgets" || adjusted.mutations[0]?.budgets?.max_output_tokens !== 700000 || adjusted.revision !== 2) throw new Error(`授权预算变更失败: ${JSON.stringify(adjusted)}`);

  const takeover = await win.webContents.executeJavaScript(`(async () => {
    window.__agentLoopTest.snapshot = { ...window.__agentLoopTest.snapshot, status: 'waiting_user', health: 'blocked', waiting_reason: '必须由用户决定是否接受冲突结果' };
    await openLoopView();
    const form = document.querySelector('#agentLoopOverrideForm');
    form.elements.goal.value = '优先完成测试与发布';
    form.elements.taskContract.value = '停止非关键优化';
    form.elements.criteria.value = '测试通过\\n发布证据完整';
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    for (let count = 0; count < 150 && !document.querySelector('.loop-dashboard')?.textContent.includes('优先完成测试与发布'); count += 1) await new Promise(next => setTimeout(next, 20));
    return { overrides: window.__agentLoopTest.overrides, snapshot: loopStore.get().snapshot, text: document.querySelector('.loop-dashboard')?.textContent || '' };
  })()`);
  if (takeover.overrides.length !== 1 || takeover.snapshot.goal_revision !== 2 || takeover.snapshot.authority_revision !== 3 || takeover.snapshot.status !== "running") throw new Error(`用户接管优先级失败: ${JSON.stringify(takeover)}`);

  const completed = await win.webContents.executeJavaScript(`(() => {
    const final = { ...window.__agentLoopTest.snapshot, status: 'completed', health: 'idle', current_round_id: 'round-14', final_result: { final_path: [{ context_id: 'root', revision_id: 'root-r7' }], unadopted_lanes: [{ lane_id: 'abandoned', context_id: 'extra-1', reason: '未采用探索' }] } };
    window.__agentLoopTest.snapshot = final;
    loopStore.reconcile(final);
    loopStore.reconcileRelated({ ...loopStore.get().related });
    renderLoop();
    return { text: document.querySelector('.loop-dashboard')?.textContent || '', controls: document.querySelectorAll('[data-action="loop-control"]').length, result: loopStore.get().snapshot.final_result };
  })()`);
  if (completed.controls !== 0 || completed.result.final_path[0].revision_id !== "root-r7" || completed.result.unadopted_lanes[0].lane_id !== "abandoned" || !completed.text.includes("completed")) throw new Error(`完成与未采用路径保留失败: ${JSON.stringify(completed)}`);

  console.log("agent-loop-ui-e2e: 启动离开、重连、多 Lane、委托来源、接管、等待与完成通过");
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
