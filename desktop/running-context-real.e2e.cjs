/* 本文件对外提供四个真实 Context Run 的 Electron HTTP/SSE 预览验收。
 * 输入为 Python 隔离服务的工作区、Context/Run 身份和模型屏障；输出为角色、工具等待/结果、并行隔离、重连游标及终态截图和 JSON。
 * 工作流为打开生产全图，经测试路由只推进模型采样，旁听真实 EventSource 并核对固定卡片、尾部和任务接入；示例：由 test_running_context_preview_real.py 启动。
 */
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-running-real-")));
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1440, height: 1000, webPreferences: {
    contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    preload: path.join(__dirname, "running-context-real-preload.cjs"),
  } });
  const config = JSON.parse(process.env.FOCUS_RUNNING_REAL_CONFIG);
  const execute = code => win.webContents.executeJavaScript(code, true);
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const shot = async name => {
    await delay(200); win.webContents.invalidate(); await delay(100);
    fs.writeFileSync(path.join(process.env.FOCUS_TASK_REAL_EVIDENCE, name + ".png"), (await win.webContents.capturePage()).toPNG());
  };
  const until = async code => {
    const deadline = Date.now() + 20000;
    while (!await execute(`Boolean(${code})`)) {
      if (Date.now() > deadline) {
        await shot("real-running-failure");
        fs.writeFileSync(path.join(process.env.FOCUS_TASK_REAL_EVIDENCE, "real-running-failure.json"), JSON.stringify(await execute('({body:document.body.innerText,events:realRunEvents, sources:realRunSources.map(x=>({url:x.url,state:x.source.readyState}))})')));
        throw Error("UI timeout: " + code);
      }
      await delay(40);
    }
  };
  win.webContents.on("console-message", (_event, level, message) => { if (level >= 3) console.error(message); });
  const control = payload => execute(`fetch('/desktop/api/__test__/running-preview',{method:${JSON.stringify(payload ? "POST" : "GET")},headers:{'Content-Type':'application/json','X-Focus-Session':'focus-dev-session'},${payload ? `body:${JSON.stringify(JSON.stringify(payload))},` : ""}}).then(async r=>{if(!r.ok)throw Error(await r.text());return r.json()})`);
  const settled = async () => {
    const deadline = Date.now() + 15000;
    let value;
    do {
      value = await control();
      if (value.runs.every(run => !["pending", "running"].includes(run.status))) break;
      await delay(50);
    } while (Date.now() < deadline);
    assert.ok(value.runs.every(run => run.status === "success"), JSON.stringify(value));
    return value;
  };
  await win.loadURL(process.env.FOCUS_TASK_REAL_URL + "/desktop/");
  await until('state.tasks.length && document.body.dataset.view === "patrol"');
  await execute(`state.patrolWorkspace={workspace_id:${JSON.stringify(config.workspace_id)},display_name:'并行真实运行验收'};document.querySelector('[data-action=show-map]').click()`);
  await until('document.querySelectorAll("[data-live-run-id]").length === 4');
  const initial = await control();
  assert.equal(initial.controls.length, 4);
  assert.ok(initial.runs.every(run => run.status === "running"));
  const readCards = () => execute('Array.from(document.querySelectorAll("[data-live-run-id]"),node=>({run_id:node.dataset.liveRunId,context_id:node.dataset.contextId,role:node.querySelector(".context-preview-role").textContent,text:node.querySelector(".context-preview-text").textContent,status:node.querySelector(".context-preview-status").textContent,height:node.offsetHeight}))');
  const result = { config, initial: await readCards() };
  await shot("real-running-waiting");
  await control({ phase: "assistant" });
  await until('Array.from(document.querySelectorAll(".context-preview-text")).filter(n=>n.textContent.includes("实时输出")).length === 4');
  result.assistant = await readCards();
  assert.ok(result.assistant.every(card => card.role === "Assistant"));
  assert.notEqual(result.assistant[0].run_id, result.assistant[1].run_id);
  result.connection_budget = await execute(`(async()=>{
    const started=performance.now();
    const response=await fetch('/desktop/api/tasks',{headers:{'X-Focus-Session':'focus-dev-session'}});
    const tasks=await response.json();
    return {run_streams:realRunSources.filter(item=>item.url.includes('/runs/')&&item.source.readyState===1).length,
      http_status:response.status,task_count:tasks.length,http_ms:performance.now()-started};
  })()`);
  result.connection_budget.live_streams = (await control()).connections.filter(value => value.endsWith("/live/stream")).length;
  assert.equal(result.connection_budget.run_streams, 4);
  assert.equal(result.connection_budget.live_streams, 1);
  assert.equal(result.connection_budget.http_status, 200);
  assert.ok(result.connection_budget.http_ms < 5000);
  await shot("real-running-assistant");
  await control({ phase: "disconnect", run_id: config.run_ids[0] });
  await until('document.querySelector("[data-preview-status=disconnected]")');
  await shot("real-running-reconnecting");
  await control({ phase: "tool" });
  await until('Array.from(document.querySelectorAll(".context-preview-role")).filter(n=>n.textContent.includes("read_file")).length === 4');
  result.tool_wait = await readCards();
  assert.ok(result.tool_wait.every(card => !card.text.includes("实时输出")));
  await shot("real-running-tool-waiting");
  await control({ phase: "result" });
  await until('Array.from(document.querySelectorAll(".context-preview-text")).filter(n=>n.textContent.includes("工具结果最后一行")).length === 4');
  result.tool_result = await readCards();
  await shot("real-running-tool-result");
  await control({ phase: "final" });
  await until('Array.from(document.querySelectorAll(".context-preview-text")).filter(n=>n.textContent.includes("最终一行")).length === 4');
  result.final = await readCards();
  for (const card of result.final) assert.equal(card.height, result.initial.find(item => item.run_id === card.run_id).height);
  await shot("real-running-final-assistant");
  await control({ phase: "finish" });
  await until('!document.querySelector("[data-live-run-id]")');
  result.finished = await settled();
  await shot("real-running-terminal");
  result.replacement = await control({ phase: "restart" });
  await until(`document.querySelector('[data-live-run-id="${result.replacement.run_id}"]')`);
  assert.equal((await readCards())[0].text, "");
  await control({ phase: "assistant", key: "4" });
  await until('document.querySelector(".context-preview-text")?.textContent.includes("实时输出")');
  result.replaced_card = await readCards();
  assert.ok(result.replaced_card[0].text.includes("线程 4"));
  await shot("real-running-replacement");
  for (const phase of ["tool", "result", "final", "finish"]) await control({ phase, key: "4" });
  await until('!document.querySelector("[data-live-run-id]")');
  result.finished = await settled();
  result.events = await execute('realRunEvents');
  result.sources = await execute('realRunSources.map(item=>({url:item.url,state:item.source.readyState}))');
  fs.writeFileSync(process.env.FOCUS_RUNNING_REAL_RESULT, JSON.stringify(result));
  win.destroy(); app.quit();
}).catch(error => { console.error(error); app.exit(1); });
