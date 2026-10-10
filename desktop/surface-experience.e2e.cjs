/* 本文件对外提供现有 Focus 页面材质、连续性和硬件渲染的隔离验收。
 * 输入为 FOCUS_SURFACE_EVIDENCE、before/after 阶段和已有离线网络 fixture；输出为实际截图、时刻帧、Chromium trace 与指标。
 * 工作流为独立 userData 的正常 GPU Electron 加载生产页面，通过可信鼠标事件测量，再检查反向、草稿、压力更新与降级；fixture 不证明真实业务执行。
 * 示例：FOCUS_SURFACE_PHASE=before electron surface-experience.e2e.cjs。真实 HTTP/SSE 另由原 PostgreSQL 用例验证。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { app, BrowserWindow, contentTracing } = require("electron");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-surface-")));
const output = process.env.FOCUS_SURFACE_EVIDENCE;
const after = process.env.FOCUS_SURFACE_PHASE === "after";
const coreOnly = process.env.FOCUS_SURFACE_CORE_ONLY === "1";
fs.mkdirSync(output, { recursive: true });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: true, width: 1280, height: 900, webPreferences: {
    preload: path.join(__dirname, "workspace-patrol-test-preload.cjs"),
    contextIsolation: false, sandbox: false, backgroundThrottling: false,
  } });
  const errors = [];
  win.webContents.on("console-message", (_event, _level, message) => { if (message?.includes("Uncaught")) errors.push(message); });
  const run = code => win.webContents.executeJavaScript(code, true);
  const until = async code => {
    const end = Date.now() + 15000;
    while (!await run(`Boolean(${code})`)) { if (Date.now() > end) throw new Error(`Timeout: ${code} ${await run('JSON.stringify({hidden:document.querySelector("[data-patrol-details-content]")?.hidden,nodes:document.querySelectorAll("[data-patrol-lineage] [data-context-id]").length,error:document.querySelector("[data-patrol-error]")?.textContent})')}`); await sleep(20); }
  };
  const capture = async name => {
    fs.writeFileSync(path.join(output, name + ".png"), (await win.webContents.capturePage()).toPNG());
    return await run(`({time:performance.now(), name:${JSON.stringify(name)}, animations:document.getAnimations().length,selection:[surfaceInput.selectionStart,surfaceInput.selectionEnd]})`);
  };
  const click = async selector => {
    if (!win.isFocused()) { win.focus(); await sleep(30); }
    const point = await run(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
    win.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...point });
    win.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...point });
  };
  const toggle = "[data-patrol-composer] [data-patrol-details]";
  await win.loadFile(path.join(process.env.FOCUS_SURFACE_APP_DIR || __dirname, "index.html"));
  await until('document.querySelector("[data-patrol-content]")');
  await run(`state.patrolWorkspace={workspace_id:'surface',display_name:'Focus 工作台验收',path:'C:/isolated/focus-surface'};render()`);
  await until('!document.querySelector("[data-patrol-content]").disabled');
  await run(`document.querySelector('[data-patrol-content]').value='确认当前范围，保持工作线独立。';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
  await until('document.querySelector("[data-patrol-receipt]").textContent.includes("已受理")');
  await until('window.patrolFixture.connected("loop-surface")');
  const manifest = { roots: {}, nodes: ["root", "research", "implementation"].map((id, index) => ({ context_id: id, revision_id: "r" + (index + 1), generation: index + 1 })), edges: [
    { source_context_id: "root", source_revision_id: "r1", target_context_id: "research", target_revision_id: "r2" },
    { source_context_id: "research", source_revision_id: "r2", target_context_id: "implementation", target_revision_id: "r3" },
  ], complete: true };
  await run(`window.patrolFixture.lineage=${JSON.stringify(manifest)};
    for(const [id,title] of [['root','需求与边界'],['research','研究和证据'],['implementation','实现与验证']]) window.patrolFixture.emit('loop-surface','context',id,1,{title,status:'active',purpose:title,current_revision_id:'r2'});
    document.querySelector('[data-patrol-content]').value='未发送的想法：保留输入和阅读位置。';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));
    window.surfaceForm=document.querySelector('[data-patrol-composer]');window.surfaceInput=document.querySelector('[data-patrol-content]');surfaceInput.setSelectionRange(2,5);
    window.surfaceMetrics={events:[],longtasks:[],frames:[]};
    new PerformanceObserver(list=>surfaceMetrics.longtasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:false});
    new PerformanceObserver(list=>surfaceMetrics.events.push(...list.getEntries().filter(e=>e.name==='click').map(e=>({start:e.startTime,duration:e.duration,processing:e.processingEnd-e.processingStart,interactionId:e.interactionId})))).observe({type:'event',durationThreshold:16});`);
  if (process.env.FOCUS_SURFACE_LIVE_FRAMES === "1") {
    await click(toggle);await sleep(350);
    await run(`window.patrolFixture.lineage={roots:{},complete:true,nodes:Array.from({length:128},(_,i)=>({context_id:'load-'+i,revision_id:'load-r'+i,generation:1})),edges:Array.from({length:256},(_,i)=>({source_context_id:'load-'+(i%128),target_context_id:'load-'+((i+1)%128),source_revision_id:'r1',target_revision_id:'r2'}))};window.patrolFixture.emit('loop-surface','portfolio','measure',128,{current_portfolio_revision_id:'measure-128'});window.patrolFixture.emit('loop-surface','context','load-0',129,{title:'visible-update-0',status:'active'});for(let i=0;i<200;i++)window.patrolFixture.emit('loop-surface','fact','fact-'+i,128,{fact_type:'test',state:'observed',presentation:{title:'受控事实 '+i,outcome_status:'unknown'}})`);
    await until('document.querySelectorAll("[data-patrol-lineage] [data-context-id]").length===128 && document.querySelectorAll("[data-patrol-facts] tr[data-fact-id]").length===200');
    await run(`document.querySelector('[data-context-id="load-0"]').scrollIntoView({block:'nearest'})`);await sleep(350);
    const rect=await run(`(()=>{const r=document.querySelector('[data-context-id="load-0"] .context-node-top').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,dpr:devicePixelRatio}})()`);
    const crypto=require('node:crypto'), rows=[];let pending=null, prior=null, skips=0, i=0;
    const roi={x:Math.floor(rect.x*rect.dpr),y:Math.floor(rect.y*rect.dpr),width:Math.floor(rect.width*rect.dpr),height:Math.floor(rect.height*rect.dpr)};
    const pixels=Buffer.alloc(roi.width*roi.height*4);
    const baseline=await win.webContents.capturePage(), fullSize=baseline.getSize();
    const digest = (frame, dirty={x:0,y:0,width:fullSize.width,height:fullSize.height}) => {
      const {width,height}=frame.getSize(), bitmap=frame.toBitmap();
      const left=Math.max(roi.x,dirty.x), top=Math.max(roi.y,dirty.y), right=Math.min(roi.x+roi.width,dirty.x+dirty.width), bottom=Math.min(roi.y+roi.height,dirty.y+dirty.height);
      const ox=width===fullSize.width?0:dirty.x, oy=height===fullSize.height?0:dirty.y;
      if(left<right && top<bottom && left-ox>=0 && right-ox<=width && top-oy>=0 && bottom-oy<=height){
        for(let row=top;row<bottom;row++)bitmap.copy(pixels,((row-roi.y)*roi.width+left-roi.x)*4,((row-oy)*width+left-ox)*4,((row-oy)*width+right-ox)*4);
      }
      return crypto.createHash('sha256').update(pixels).digest('hex');
    };
    prior=digest(baseline);
    await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(new Error('Native presentation frames timed out')),20000);
      win.webContents.beginFrameSubscription(true, (frame,dirty)=>{
        const next=digest(frame,dirty);
        if(!prior){prior=next;return;}
        if(pending && next!==prior){
          rows.push({index:pending.index,duration:Number(process.hrtime.bigint()-pending.start)/1e6});
          if([6,25,55,85,105].includes(pending.index))fs.writeFileSync(path.join(output,'present-'+pending.index+'.png'),frame.toPNG());
          pending=null;
          if(rows.length===105){clearTimeout(timeout);clearInterval(timer);resolve();}
        }
        prior=next;
      });
      const timer=setInterval(()=>{
        if(!prior)return;
        if(pending){skips++;return;}
        pending={index:++i,start:process.hrtime.bigint()};
        void run(`window.patrolFixture.emit('loop-surface','context','load-0',${129+i},{title:'visible-update-${i}',status:'active'})`).catch(reject);
      },50);
    });
    win.webContents.endFrameSubscription();
    const sorted=rows.slice(5).map(row=>row.duration).sort((a,b)=>a-b);
    const report={fixture:true,method:'enqueue-to-presented-pixel-change; only dirty frames; one pending update; incremental title-pixel SHA256; includes IPC and readback; 5 warmups',nodes:128,facts:200,n:sorted.length,p95:sorted[Math.ceil(sorted.length*.95)-1],max:sorted.at(-1),skippedTicks:skips,rect,rows,gpu:await app.getGPUInfo('complete')};
    fs.writeFileSync(path.join(output,'live-presentation.json'),JSON.stringify(report,null,2));win.destroy();app.quit();return;
  }
  await sleep(400);
  await capture("quiet");
  await contentTracing.startRecording({ included_categories: ["devtools.timeline", "blink.user_timing", "input", "latencyInfo", "cc", "disabled-by-default-devtools.timeline"] });
  for (let group = 0; group < (coreOnly ? 0 : 3); group++) {
    for (let i = 0; i < 5; i++) { await click(toggle); await sleep(280); }
    await run(`performance.mark('local-group-${group}')`);
    for (let i = 0; i < 30; i++) { await click(toggle); await sleep(280); }
  }
  if (!await run('document.querySelector(".workspace-patrol").classList.contains("is-quiet")')) { await click(toggle); await sleep(300); }
  const frames = [];
  frames.push(await capture("closed-loop-quiet"));
  await click(toggle);
  for (let i = 0; i < 7; i++) { frames.push(await capture("expand-" + i)); await sleep(30); }
  await until('document.querySelector("[data-patrol-lineage] [data-context-id=root]")');
  await sleep(300);
  await capture("details");
  await click("[data-patrol-lineage] [data-context-id=root]");
  for (let i = 0; i < 7; i++) { frames.push(await capture("drawer-" + i)); await sleep(30); }
  await until('document.querySelector("[data-patrol-context-close]")');
  await sleep(300);
  await capture("context");
  await click("[data-patrol-context-close]");
  for (let i = 0; i < 7; i++) { frames.push(await capture("drawer-close-" + i)); await sleep(30); }
  await click(toggle);
  for (let i = 0; i < 7; i++) { frames.push(await capture("collapse-" + i)); await sleep(30); }
  await run(`document.querySelector(${JSON.stringify(toggle)}).focus({preventScroll:true})`);
  const keyToggle = () => { win.focus(); win.webContents.sendInputEvent({type:'keyDown',keyCode:'Space'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Space'}); };
  for (const interval of [40, 80, 120]) for (let i = 0; i < 7; i++) { keyToggle(); await sleep(interval); keyToggle(); await sleep(300); }
  if (after) assert.equal(await run('document.querySelector("[data-patrol-details-content]").hidden && document.querySelector("[data-patrol-details-content]").inert'), true);
  assert.equal(await run('surfaceForm===document.querySelector("[data-patrol-composer]") && surfaceInput===document.querySelector("[data-patrol-content]")'), true);
  assert.equal(await run('surfaceInput.value'), "未发送的想法：保留输入和阅读位置。");
  assert.deepEqual(await run('[surfaceInput.selectionStart,surfaceInput.selectionEnd]'), [2, 5]);
  const stress = [];
  if (await run('document.querySelector(".workspace-patrol").classList.contains("is-quiet")')) await click(toggle);
  await sleep(350);
  for (const count of (coreOnly ? [] : [32, 128])) {
    await run(`window.patrolFixture.lineage={roots:{},complete:true,nodes:Array.from({length:${count}},(_,i)=>({context_id:'load-'+i,revision_id:'load-r'+i,generation:1})),edges:Array.from({length:${count * 2}},(_,i)=>({source_context_id:'load-'+(i%${count}),target_context_id:'load-'+((i+1)%${count}),source_revision_id:'r1',target_revision_id:'r2'}))};
      window.patrolFixture.emit('loop-surface','portfolio','load',${count},{current_portfolio_revision_id:'load-${count}'});`);
    await until(`document.querySelectorAll('[data-patrol-lineage] [data-context-id]').length===${count}`);
    const result = await run(`(async()=>{const intervals=[];let last=performance.now();const start=last;let finish=last+2000;
      await new Promise(resolve=>{const tick=t=>{intervals.push(t-last);last=t;if(t<finish)requestAnimationFrame(tick);else resolve()};requestAnimationFrame(tick)});
      return {nodes:${count},edges:${count * 2},start,end:performance.now(),intervals};})()`);
    stress.push(result);
    await run(`for(let i=0;i<200;i++)window.patrolFixture.emit('loop-surface','fact','fact-'+i,${count},{fact_type:'test',state:'observed',presentation:{title:'受控事实 '+i,outcome_status:'unknown'}})`);
    result.updatesStart=await run("performance.now()");
    const injections=[];
    await new Promise(resolve => { let i=0;const timer=setInterval(()=>{
      injections.push(run(`window.patrolFixture.emit('loop-surface','context','load-${i % count}',${count + i + 1},{title:'受控节点 ${i % count}',status:'active'})`));
      if(++i===100){clearInterval(timer);resolve();}
    },50); });
    await Promise.all(injections);
    result.updatesEnd=await run("performance.now()");
  }
  let conversation = null;
  if (!coreOnly) {
    await run(`state.view='focus';render()`);
    await until('document.querySelector("#mainInput") && state.details.has(state.activeTaskId)');
    await run(`const detail=state.details.get(state.activeTaskId);detail.messages=Array.from({length:1000},(_,i)=>({id:'pressure-'+i,role:i%2?'ai':'human',content:'会话压力 '+i+' '+ '受控长正文 '.repeat(25)}));render()`);
    const begin=await run('performance.now()');
    for(let i=0;i<100;i++) {
      const point=await run(`(()=>{const r=document.querySelector('.conversation').getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
      win.focus(); win.webContents.sendInputEvent({type:'mouseWheel',deltaY:i%20<10?-120:120,deltaX:0,...point});
      if(i%10===0) await run(`document.querySelector('#mainInput').value='保留任务草稿 ${i}';document.querySelector('#mainInput').dispatchEvent(new Event('input',{bubbles:true}))`);
      await sleep(200);
    }
    conversation=await run(`({start:${begin},end:performance.now(),messages:state.details.get(state.activeTaskId).messages.length,rendered:document.querySelectorAll('.work-record').length,draft:document.querySelector('#mainInput').value})`);
    await run(`state.view='patrol';render()`);await until('document.querySelector("[data-patrol-content]")');
    await run(`surfaceForm=document.querySelector('[data-patrol-composer]');surfaceInput=document.querySelector('[data-patrol-content]')`);
  }
  await run("surfaceInput.focus();surfaceInput.setSelectionRange(2,5)");
  if (!coreOnly) {
    if(await run('document.querySelector(".workspace-patrol").classList.contains("is-quiet")')) {await click(toggle);await sleep(350);}
    await until('document.querySelectorAll("[data-patrol-lineage] [data-context-id]").length===128');await click(toggle);await sleep(350);
  }
  const idleNodes=await run('document.querySelectorAll("*").length');
  const idleStart = await run('performance.now()'); await sleep(coreOnly ? 0 : 10000);
  const idle = await run(`({start:${idleStart},end:performance.now(),animations:document.getAnimations().length,inputRetained:surfaceInput===document.querySelector('[data-patrol-content]')})`);
  await run(`document.querySelector(${JSON.stringify(toggle)}).focus({preventScroll:true})`);
  for (let i = 0; i < (coreOnly ? 0 : 50); i++) { keyToggle(); await sleep(40); }
  await sleep(350); await sleep(coreOnly ? 0 : 10000);
  const terminal = await run(`({nodes:document.querySelectorAll("*").length,idleNodes:${idleNodes},animations:document.getAnimations().length,detailsHidden:document.querySelector('[data-patrol-details-content]').hidden,detailsInert:document.querySelector('[data-patrol-details-content]').inert,drawerHidden:document.querySelector('[data-patrol-context]').hidden})`);
  const trace = await contentTracing.stopRecording(path.join(output, "renderer-trace.json"));
  win.webContents.debugger.attach("1.3");
  for (const [name, features] of [["reduced", [{name:"prefers-reduced-motion",value:"reduce"}]], ["opaque", [{name:"prefers-reduced-transparency",value:"reduce"}]], ["contrast", [{name:"forced-colors",value:"active"}]]]) {
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features });
    await sleep(100); await capture(name);
    await click(toggle); await sleep(300); await click(toggle); await sleep(300);
  }
  await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [] });
  for (const width of [900, 640, 390]) { win.setContentSize(width, 820); await sleep(300); await capture("width-" + width); assert.equal(await run('document.documentElement.scrollWidth>innerWidth'), false); }
  win.setContentSize(1280, 820); win.webContents.setZoomFactor(2); await sleep(300); await capture("zoom-200");
  win.webContents.setZoomFactor(1);
  const metrics = await run('({...surfaceMetrics,viewport:[innerWidth,innerHeight],dpr:devicePixelRatio})');
  const report = { phase: after ? "after" : "before", runtime: process.versions, gpu: await app.getGPUInfo("complete"), hardwareAccelerationEnabled: app.isHardwareAccelerationEnabled(), graphics: app.getGPUFeatureStatus(), metrics, frames, stress, conversation, idle, terminal, errors, trace, fixture: true };
  fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log("SURFACE_EXPERIENCE_PASS " + path.join(output, "report.json"));
  win.destroy(); app.quit();
}).catch(error => { fs.writeFileSync(path.join(output, "failure.txt"), error.stack); console.error(error); app.exit(1); });
