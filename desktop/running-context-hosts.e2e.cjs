/* 本文件对外提供两个生产图宿主的运行预览生命周期浏览器验收。
 * 输入为独立 preview-test-preload 的可控 HTTP/SSE 协议 fixture；输出为实时角色、工具等待/结果、终态、身份隔离、精确历史与 Main 共享连接断言。
 * 工作流为加载生产 index/app，沿 Patrol/全图的页面入口和已有 Live 接口驱动状态；传输替身不进入生产，真实服务端协议由独立后端集成验收覆盖。
 * 示例：node desktop/running-context-hosts.e2e.cjs；FOCUS_PREVIEW_HOST_EVIDENCE 指定可选宿主截图目录。
 */
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 120000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-preview-hosts-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 960, webPreferences: { preload: path.join(__dirname, "preview-test-preload.cjs"), contextIsolation: false, sandbox: false, offscreen: true, backgroundThrottling: false } });
    const errors = [];
    win.webContents.on("console-message", event => { if (event.message?.includes("Uncaught")) errors.push(event.message); });
    win.webContents.on("preload-error", (_event, _file, error) => errors.push(error.stack));
    const run = async script => { try { return await win.webContents.executeJavaScript(script, true); } catch (error) { throw new Error(script + "\n" + error.message); } };
    const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async expression => { const deadline = Date.now() + 10000; while (!await run(`Boolean(${expression})`)) { if (Date.now() > deadline) throw new Error("Timeout: " + expression + "\n" + errors.join("\n") + "\n" + await run("document.body.innerText.slice(0,1500)")); await pause(25); } };
    const click = async selector => {
      const point = await run(`(()=>{const element=[...document.querySelectorAll(${JSON.stringify(selector)})].find(node=>node.getBoundingClientRect().width>0&&!node.closest('[inert]'));if(!element)throw Error('No visible control');element.scrollIntoView({block:'nearest',inline:'nearest'});const r=element.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);
      const x = Math.round(point.x * win.webContents.getZoomFactor()), y = Math.round(point.y * win.webContents.getZoomFactor());
      win.webContents.sendInputEvent({type:"mouseDown",x,y,button:"left",clickCount:1});win.webContents.sendInputEvent({type:"mouseUp",x,y,button:"left",clickCount:1}); await pause(40);
      await run("Promise.all(document.getAnimations().filter(animation=>Number.isFinite(animation.effect.getComputedTiming().endTime)).map(animation=>animation.finished.catch(()=>{})))");
    };
    const body = "document.querySelector('[data-live-run-id=\"workspace-run-0\"] .context-preview-text')";
    const role = "document.querySelector('[data-live-run-id=\"workspace-run-0\"] .context-preview-role')";
    const status = "document.querySelector('[data-live-run-id=\"workspace-run-0\"] [data-context-preview]')?.dataset.previewStatus";
    const emit = (type, data) => run(`previewFixture.event('workspace-run-0',${JSON.stringify(type)},${JSON.stringify(data)})`);
    const messages = (...rows) => ({ messages: [{ role: "human", id: "input-workspace-run-0", content: "本轮输入" }, ...rows] });
    const capture = async name => { if (!process.env.FOCUS_PREVIEW_HOST_EVIDENCE) return; await run("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))"); await pause(200); fs.mkdirSync(process.env.FOCUS_PREVIEW_HOST_EVIDENCE, {recursive:true}); fs.writeFileSync(path.join(process.env.FOCUS_PREVIEW_HOST_EVIDENCE,name+".png"),(await win.webContents.capturePage()).toPNG()); };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until("document.querySelector('[data-patrol-content]')");
    await run("state.patrolWorkspace={workspace_id:'workspace',display_name:'运行预览验收',path:'C:/workspace'};render()");
    await until("patrolFixture.connected('loop-workspace')");
    await click("[data-patrol-composer] [data-patrol-details]");
    await until("document.querySelectorAll('[data-patrol-lineage] .workbench-node').length===8");
    assert.equal(await run("previewFixture.count()"), 0);
    await run("for(let i=0;i<8;i++)previewFixture.runState('workspace-run-'+i,'running')");
    await until("previewFixture.count()===8 && document.querySelectorAll('[data-live-run-id]').length===8");
    await run("previewFixture.runs['later-queued']={...previewFixture.runs['workspace-run-0'],run_id:'later-queued',status:'queued'};previewFixture.runState('later-queued','queued')");
    await pause(80); assert.equal(await run("previewFixture.count()"),8);assert.equal(await run("document.querySelector('[data-live-run-id=workspace-run-0]')!==null"),true);
    await emit("events", messages());
    await emit("tokens", { message_id: "assistant-1", content: "先读取工作区结构。" });
    await until(`${body}?.textContent==='先读取工作区结构。'`); assert.equal(await run(`${role}.textContent`), "Assistant");
    const taskBefore = await run("state.activeTaskId");
    await emit("events", messages({ role:"ai",id:"assistant-1",content:"先读取工作区结构。",tool_calls:[{id:"call-1",name:"read_file",args:{path:"README.md"}}] }));
    await until(`${status}==='tool_wait'`); assert.equal(await run(`${role}.textContent`), "Tool · read_file"); assert.equal(await run(`${body}.textContent`), "");
    await emit("events", messages({ role:"ai",id:"assistant-1",content:"先读取工作区结构。",tool_calls:[{id:"call-1",name:"read_file"}] },{role:"tool",id:"tool-1",tool_call_id:"call-1",name:"read_file",content:"真实工具结果第一行\n真实工具结果第二行"}));
    await until(`${body}?.textContent.includes('真实工具结果第二行')`);
    await emit("tokens", {message_id:"assistant-2",content:"接下来确认已有模块。"});
    await until(`${role}?.textContent==='Assistant' && ${body}?.textContent==='接下来确认已有模块。'`);
    assert.equal(await run("state.activeTaskId"), taskBefore); assert.equal(await run("state.streams.size"), 0);
    await capture("patrol-assistant");
    await run("previewFixture.disconnect('workspace-run-0')"); await until(`${status}==='disconnected'`);
    await emit("tokens", {message_id:"assistant-2",content:"断线恢复。"}); await until(`${status}==='streaming'`);
    const beforeDuplicate = await run(`${body}.textContent`);
    await run("previewFixture.event('workspace-run-0','tokens',{message_id:'assistant-2',content:'重复不应出现'},{id:previewFixture.sequence['workspace-run-0']})");
    await pause(50); assert.equal(await run(`${body}.textContent`), beforeDuplicate);
    await click("[data-patrol-composer] [data-patrol-details]"); await until("previewFixture.count()===0");
    await click("[data-patrol-composer] [data-patrol-details]"); await until("previewFixture.count()===8");
    await click("[data-action=show-map]"); await until("mapLive?.store.get().projection && previewFixture.count()===8 && document.querySelectorAll('[data-map-graph] [data-live-run-id]').length===8");
    await emit("tokens", {message_id:"map-message",content:"全图角色输出"}); await until(`${body}?.textContent==='全图角色输出'`);
    await capture("map-assistant");
    await run("previewFixture.event('workspace-run-0','events',{type:'commitment_messages',actor:'worker',content_mode:'delta',stream_id:'worker-1',messages:[{role:'ai',content:'工作角色输出'}]})");
    await until(`${role}?.textContent==='Worker' && ${body}?.textContent==='工作角色输出'`);
    await emit("tokens", {message_id:"map-tool-request",content:"检查工作区文件"});
    await emit("events", messages({role:"ai",id:"map-tool-request",content:"检查工作区文件",tool_calls:[{id:"map-tool",name:"shell"}]}));
    await until(`${status}==='tool_wait'`);assert.equal(await run(`${role}.textContent`),"Tool · shell");
    await emit("events", messages({role:"tool",id:"map-result",tool_call_id:"map-tool",name:"shell",content:"实际返回的测试结果"}));
    await until(`${body}?.textContent==='实际返回的测试结果'`);
    await emit("tokens", {message_id:"map-final",content:"全图恢复Assistant正文"});await until(`${role}?.textContent==='Assistant' && ${body}?.textContent==='全图恢复Assistant正文'`);
    await run("patrolFixture.lineage.nodes.push({context_id:'workspace-ctx-0',revision_id:'old-revision',generation:0});void inspectMapContext('workspace-ctx-0',document.querySelector('[data-context-id=workspace-ctx-0]'),'old-revision')");
    await until("mapContextInspector.selection()?.revisionId==='old-revision' && !document.querySelector('[data-context-id=workspace-ctx-0]').hasAttribute('data-live-run-id')");
    await click("[data-patrol-context-close]"); await until("document.querySelector('[data-live-run-id=workspace-run-0]')");
    for (const mode of ["tree", "cards"]) { await click(`[data-map-view=${mode}]`); await until("previewFixture.count()===0"); await click("[data-map-view=graph]"); await until("previewFixture.count()===8"); }
    await run("previewFixture.event('workspace-run-0','tokens',{message_id:'main-replay',content:'图先收到、任务后接入的正文'});listenToRun(previewFixture.runs['workspace-run-0'])");
    await until("state.streams.has('workspace-run-0') && state.streamBuffers.get('workspace-run-0')?.text==='图先收到、任务后接入的正文'");
    assert.equal(await run("previewFixture.count('workspace-run-0')"), 1);
    await click("[data-map-view=tree]"); await until("previewFixture.count()===1");
    await emit("tokens", {message_id:"main-replay",content:" · 图退出后任务继续"});
    await until("state.streamBuffers.get('workspace-run-0')?.text.endsWith('图退出后任务继续')");
    await run("state.streams.get('workspace-run-0').close();state.streams.delete('workspace-run-0');clearStreamBuffer('workspace-run-0')"); await until("previewFixture.count()===0");
    await click("[data-map-view=graph]"); await until("previewFixture.count()===8");
    await click("[data-context-id=workspace-ctx-0]");await until("document.querySelector('[data-patrol-open-task=workspace-ctx-0]')");
    await click("[data-patrol-open-task=workspace-ctx-0]");
    await until("state.view==='focus' && state.activeTaskId==='workspace-ctx-0' && state.streamBuffers.get('workspace-run-0')?.text.endsWith('图退出后任务继续')");
    assert.equal(await run("previewFixture.count('workspace-run-0')"),1);
    await click("[data-action=show-map]");await until("previewFixture.count()===8");
    await run("state.streams.get('workspace-run-0').close();state.streams.delete('workspace-run-0');clearStreamBuffer('workspace-run-0')");
    await run("previewFixture.replaceRun('workspace-run-0','replacement-run')"); await until("document.querySelector('[data-live-run-id=replacement-run]') && previewFixture.count('replacement-run')===1 && previewFixture.count('workspace-run-0')===0");
    await run("previewFixture.event('workspace-run-0','tokens',{message_id:'old-late',content:'旧Run迟到正文'},{late:true});previewFixture.event('replacement-run','tokens',{message_id:'new-message',content:'新Run正文'})");
    await until("document.querySelector('[data-live-run-id=replacement-run] .context-preview-text')?.textContent==='新Run正文'");
    await run("previewFixture.runs['replacement-run'].status='success';previewFixture.event('replacement-run','end',{status:'success'})");
    await until("!document.querySelector('[data-live-run-id=replacement-run]') && previewFixture.count('replacement-run')===0");
    await run("patchMapGraph()"); assert.equal(await run("previewFixture.count('replacement-run')"), 0);
    await run("state.patrolWorkspace={workspace_id:'other',display_name:'另一个工作区',path:'C:/other'};render()");
    await until("mapPortfolio.workspaceId==='other' && previewFixture.count()===2 && document.querySelector('[data-live-run-id=other-run-0]')");
    await run("previewFixture.event('workspace-run-1','tokens',{message_id:'foreign',content:'旧工作区迟到'},{late:true});previewFixture.event('other-run-0','tokens',{message_id:'other-message',content:'独立工作区正文'})");
    await until("document.querySelector('[data-live-run-id=other-run-0] .context-preview-text')?.textContent==='独立工作区正文'");
    assert.equal(await run("document.querySelector('[data-map-graph]').textContent.includes('旧工作区迟到')"), false);
    await click("[data-map-view=tree]");await run("previewFixture.denied.add('other-run-1')");await click("[data-map-view=graph]");
    await until("document.querySelector('[data-live-run-id=other-run-1] [data-context-preview]')?.dataset.previewStatus==='unavailable' && previewFixture.count()===1");
    await run("previewFixture.denied.delete('other-run-1')");await click("[data-action=refresh-map-portfolio]");await until("previewFixture.count()===2");
    await run("previewFixture.hold.add('late-query');previewFixture.replaceRun('other-run-1','late-query')");
    await until("previewFixture.held['late-query'] && document.querySelector('[data-live-run-id=late-query]')");
    await click("[data-action=show-patrol]"); await until("previewFixture.count()===0");
    await run("previewFixture.held['late-query']();previewFixture.event('late-query','tokens',{message_id:'after-leave',content:'离页后不应订阅'})");
    await pause(80);assert.equal(await run("previewFixture.count()"),0);
    assert.deepEqual(errors, []);
    console.log("PASS running preview hosts: 8 concurrent, Patrol/Map roles, tool states, reconnect/dedupe, historical exclusion, mode/workspace/run disposal, Main replay/shared transport");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
