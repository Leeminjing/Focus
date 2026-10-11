/* 本文件对外提供生产任务三栏与真实 Main/材料链的 Electron 验收。
 * 输入为隔离 HTTP 服务、任务/材料/已发布关系身份及截图目录；输出为全图精确版本检查、显式任务打开、真实文件预览、Run/SSE、记忆与独立会话断言。
 * 具体工作流为加载生产 index，显式进入任务，预览磁盘材料并提交，检查真实终态会话，再进入独立会话验证身份隔离并记录窄屏布局。
 * 示例：electron frontend-task-real.e2e.cjs；模型采样由 Python 夹具替换，HTTP/运行图/存储均为生产实现。
 */
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-task-real-")));
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false,
    width: 1440,
    height: 1000,
    webPreferences: {
      contextIsolation: false,
      sandbox: false,
      backgroundThrottling: false,
      offscreen: true,
      preload: path.join(__dirname, "frontend-task-real-preload.cjs"),
    },
  });
  const execute = code => win.webContents.executeJavaScript(code, true);
  win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)console.error(message)});
  const until = async code => {
    const end = Date.now() + 20000;
    while (!await execute(`Boolean(${code})`)) {
      if (Date.now() > end) {
        await shot("real-task-failure");
        throw Error("UI timeout: " + code + " · " + await execute('document.querySelector("#app")?.textContent.slice(0,1000)'));
      }
      await new Promise(resolve => setTimeout(resolve, 30));
    }
  };
  const shot = async name => {
    await new Promise(resolve => setTimeout(resolve, 250));
    win.webContents.invalidate();
    await new Promise(resolve => setTimeout(resolve, 100));
    fs.writeFileSync(path.join(process.env.FOCUS_TASK_REAL_EVIDENCE, name + ".png"), (await win.webContents.capturePage()).toPNG());
  };
  await win.loadURL(process.env.FOCUS_TASK_REAL_URL + "/desktop/");
  await until('state.tasks.length && document.body.dataset.view === "patrol"');
  const map = JSON.parse(process.env.FOCUS_MAP_REAL);
  await execute(`state.patrolWorkspace={workspace_id:${JSON.stringify(map.workspace_id)},display_name:'真实全图验收'};document.querySelector('[data-action=show-map]').click()`);
  await until('!mapPortfolio.loading && document.querySelector(".workbench-node")');
  const previousTask = await execute('state.activeTaskId');
  await execute(`document.querySelector('[data-context-id="${map.context_id}"]').click()`);
  await until('document.querySelector("[data-patrol-context-content]").textContent.includes("全图真实发布内容")');
  assert.equal(await execute('mapContextInspector.selection().revisionId'),map.revision_id);
  assert.equal(await execute('state.activeTaskId'),previousTask);
  await shot('real-global-map-inspection');
  await execute('document.querySelector("[data-patrol-open-task]").click()');
  await until('document.body.dataset.view === "focus" && document.querySelector("#conversation")?.textContent.includes("全图真实发布内容")');
  assert.equal(await execute('state.activeTaskId'),map.context_id);
  await shot('real-global-map-task');
  await execute(`switchTask(${JSON.stringify(process.env.FOCUS_TASK_REAL_ID)})`);
  await until('document.querySelector("#mainInput") && state.materials.get(state.activeTaskId)?.length');
  assert.equal(await execute('document.querySelector(".task-workspace-list") !== null'), true);
  assert.equal(await execute('document.querySelector(".task-workspace-layout > #appInspector") !== null'), true);
  await execute(`document.querySelector('[data-material-id="${process.env.FOCUS_TASK_REAL_MATERIAL}"] [data-action=open-material]').click()`);
  await until('document.querySelector("#filePanel")?.textContent.includes("真实磁盘材料正文")');
  await shot("real-task-material");
  await execute(`document.querySelector('[data-panel-action="close"]').click()`);
  await until('!document.querySelector("#filePanel")');
  await execute(`document.querySelector('[data-material-id="${process.env.FOCUS_TASK_REAL_MATERIAL}"] [data-action=toggle-run-material]').click()`);
  await until('materialSelection().bindings.length === 1');
  await execute('document.querySelector("#mainInput").value="真实传统任务提交"; document.querySelector("#mainInput").dispatchEvent(new Event("input",{bubbles:true})); sendMain()');
  await until('taskRunOperations.get(state.activeTaskId)?.active_run?.status === "success"');
  await until('document.querySelector("#conversation").textContent.includes("真实任务响应")');
  await until('!state.details.get(state.activeTaskId)?.active_run');
  const result = await execute('({run_id:taskRunOperations.get(state.activeTaskId).active_run.run_id, materialHistory:state.materialHistory.get(state.activeTaskId), task_id:state.activeTaskId})');
  result.map={context_id:map.context_id,revision_id:map.revision_id,opened_task_id:map.context_id};
  await shot("real-task-complete");
  await execute('document.querySelector("[data-action=organize-context]").click()');
  await until('document.body.dataset.view === "compress"');
  await execute('document.querySelector("[data-action=cancel-compression]").click()');
  await until('document.body.dataset.view === "focus"');
  await execute('document.querySelector("[data-action=show-assembly]").click()');
  await until('activeTask()?.harness_mode === "assembly" && document.querySelector("#mainInput")');
  assert.notEqual(await execute('state.activeTaskId'), result.task_id);
  assert.equal(await execute('document.querySelector("#mainInput").value'), "");
  await shot("real-standalone");
  const assemblyId = await execute('state.activeTaskId');
  await execute('document.querySelector("[data-action=show-memory]").click()');
  await until('document.body.dataset.view === "memory" && !state.memory.loading');
  await execute('document.querySelector("[data-action=new-memory]").click()');
  await until('document.querySelector("[data-memory-field=content]")');
  await execute(`for (const [key,value] of Object.entries({title:"透明材质真实记忆验收",content:"使用真实 API 保存、重新加载与删除"})) { const input=document.querySelector('[data-memory-field='+key+']'); input.value=value; input.dispatchEvent(new Event('input',{bubbles:true})); } document.querySelector('[data-action=save-memory]').click()`);
  await until('!state.memory.composing && state.memory.memories.some(item=>item.title==="透明材质真实记忆验收")');
  const memoryId = await execute('state.memory.memories.find(item=>item.title==="透明材质真实记忆验收").memory_id');
  await execute('document.querySelector("[data-action=show-assembly]").click()');
  await until('activeTask()?.harness_mode === "assembly" && document.querySelector("#mainInput")');
  await execute('document.querySelector("[data-action=show-memory]").click()');
  await until('!state.memory.loading && document.querySelector(".memory-card")');
  await execute(`document.querySelector('[data-action=select-memory][data-memory-id="${memoryId}"]').click()`);
  assert.match(await execute('document.querySelector(".memory-detail-content").textContent'), /使用真实 API 保存、重新加载与删除/);
  await shot("real-memory");
  await execute(`document.querySelector('[data-action=delete-memory][data-memory-id="${memoryId}"]').click()`);
  await until(`!state.memory.memories.some(item=>item.memory_id===${JSON.stringify(memoryId)})`);
  result.memory = { created: memoryId, reloaded: true, deleted: true };
  await execute('document.querySelector("[data-action=show-assembly]").click()');
  await until('activeTask()?.harness_mode === "assembly" && document.querySelector("#mainInput")');
  assert.equal(await execute('state.activeTaskId'), assemblyId);
  win.setContentSize(900, 850);
  await execute('document.querySelector("[data-action=close-inspector]").click()');
  await shot("real-standalone-900");
  win.setContentSize(390, 844);
  await shot("real-standalone-390");
  fs.writeFileSync(process.env.FOCUS_TASK_REAL_RESULT, JSON.stringify(result));
  win.destroy(); app.quit();
}).catch(error => { console.error(error); app.exit(1); });
