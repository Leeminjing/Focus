/* 本文件对外提供 Patrol 关系工作台的隔离浏览器回归和参考尺寸截图。
 * 输入为现有离线 API fixture 与 FOCUS_WORKBENCH_EVIDENCE；输出为生产页面的真实指针/键盘检查、版本和输入身份断言、32/128 节点更新指标。
 * 工作流为独立 Electron 会话加载生产文件，隔离数据只用于视觉与交互；真实 HTTP/SSE 由 frontend-migration-real 另外验收。
 * 示例：node desktop/patrol-graph-workbench.e2e.cjs；所有截图仅包含本测试窗口。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 120000, windowsHide: true });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-workbench-")));
  const output = process.env.FOCUS_WORKBENCH_EVIDENCE || path.join(__dirname, "../openspec/changes/refactor-patrol-graph-workbench/evidence/after");
  fs.mkdirSync(output, { recursive: true });
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1748, height: 904, webPreferences: { preload: path.join(__dirname, "workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true } });
    win.setContentSize(1748, 904);
    const errors = [], metrics = [];
    win.webContents.on("console-message", (_event, _level, message) => { if (message?.includes("Uncaught")) errors.push(message); });
    const run = async code => { try { return await win.webContents.executeJavaScript(code, true); } catch(error) { throw new Error(`${code}: ${error.message}`, {cause:error}); } };
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async code => {
      const end = Date.now() + 10000;
      while (!await run(`Boolean(${code})`)) { if (Date.now() > end) throw new Error(`Timeout: ${code} ${await run('JSON.stringify({focus:document.activeElement?.outerHTML.slice(0,400),context:document.querySelector("[data-patrol-context-content]")?.textContent,error:document.querySelector("[data-patrol-error]")?.textContent})')}`); await sleep(20); }
    };
    const capture = async name => { await sleep(250); const capture = await win.webContents.capturePage(); fs.writeFileSync(path.join(output, name + ".png"), capture.resize({width:Math.round(await run('innerWidth'))}).toPNG()); };
    const pointer = (point, type, extra = {}) => win.webContents.sendInputEvent({type, x:Math.round(point.x * win.webContents.getZoomFactor()), y:Math.round(point.y * win.webContents.getZoomFactor()), ...extra});
    const click = async selector => {
      const point = await run(`(() => {const node=document.querySelector(${JSON.stringify(selector)});node.scrollIntoView({block:'nearest',inline:'nearest'});const r=node.getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
      point.x = Math.round(point.x * win.webContents.getZoomFactor()); point.y = Math.round(point.y * win.webContents.getZoomFactor());
      win.webContents.sendInputEvent({type:"mouseDown",button:"left",clickCount:1,...point});
      win.webContents.sendInputEvent({type:"mouseUp",button:"left",clickCount:1,...point});
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'graph',display_name:'looptest',path:'C:/isolated/graph'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='工作台验收';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('window.patrolFixture.connected("loop-graph")');
    await click('[data-patrol-composer] [data-patrol-details]');
    await until('document.querySelector(".loop-map-empty")');
    await capture("empty");
    await run(`window.patrolFixture.lineageError=true;window.patrolFixture.emit('loop-graph','portfolio','empty',1,{revision:'error'})`);
    await until('document.querySelector("[data-patrol-lineage-retry]")');
    await capture("error");
    const names = ["需求澄清","执行边界","任务规格","API 调研","可行性实验","工作区权限","验收标准","架构探索","边界情形","工具与执行","工具桥接","桌宠交互","窗口行为","动效验证","快捷键方案","视图状态","异步队列","存储适配","生命周期","恢复策略","会话持久化","端到端回归","键盘导航测试","工具契约测试","测试夹具","视觉快照","异常恢复测试","构建与打包","版本检查","集成验证","发布审查","使用文档"];
    const nodes = names.map((title, index) => ({context_id:`ctx-${String(index + 1).padStart(2,"0")}`,revision_id:`rev-${index + 1}`,generation:index % 9 + 1,title}));
    const pairs = [[0,2],[1,2],[2,7],[2,11],[3,9],[4,9],[5,9],[6,11],[7,11],[8,11],[9,10],[10,11],[11,12],[11,13],[11,14],[11,21],[12,29],[13,29],[14,29],[15,21],[16,18],[17,18],[18,20],[19,20],[20,21],[21,23],[22,21],[23,29],[24,21],[25,29],[26,29],[27,29],[28,29],[29,30],[29,31],[1,9],[6,29],[2,18],[3,20],[10,24],[12,25],[18,27],[20,26],[23,28]];
    const lineage = {complete:true, roots:Object.fromEntries(nodes.map(node=>[node.context_id,node.revision_id])),nodes,edges:pairs.map(([a,b])=>({source_context_id:nodes[a].context_id,source_revision_id:nodes[a].revision_id,target_context_id:nodes[b].context_id,target_revision_id:nodes[b].revision_id}))};
    await run(`window.patrolFixture.lineageError=false;window.patrolFixture.graphConversations=true;window.patrolFixture.lineage=${JSON.stringify(lineage)};
      for(const node of window.patrolFixture.lineage.nodes) window.patrolFixture.emit('loop-graph','context',node.context_id,1,{title:node.title,status:'active',current_revision_id:node.revision_id});
      window.patrolFixture.emit('loop-graph','round','r18',1,{number:18,observation_id:'observation-18'});
      window.patrolFixture.emit('loop-graph','task_progress','progress',1,{progress_id:'p18',generation:18,document:{items:${JSON.stringify(["需求与执行边界","Agent 工具协议","桌宠交互与窗口行为","端到端回归","打包与发布验证"].map((description,i)=>({description,item_id:`item-${i}`,state:i<2?'completed':i<4?'in_progress':'not_started',support:i<2?'supported':i===2?'asserted':'unknown',context_ids:i===0?[]:i===1?['ctx-10','ctx-11']:['ctx-12']})))}}});
      for(let i=0;i<3;i++)window.patrolFixture.emit('loop-graph','fact','fact-'+i,1,{kind:'run',title:['拖拽测试 · Run 已启动','桌宠交互 · 新版本已提交','工具桥接 · Run 已结束'][i],summary:['读取已提交版本，开始运行检查','Run 产出已提交为新版本，保留来源可追溯','产生运行结果，不等于整体任务已完成'][i],status:'observed',outcome_status:'unknown',occurred_at:'2026-10-10T10:42:0'+i+'Z'});
      document.querySelector('[data-patrol-lineage-retry]').click();`);
    await until('document.querySelectorAll("[data-patrol-lineage] [data-context-id]").length===32');
    await run(`document.querySelector('[data-portfolio-zoom=fit]').click()`);
    await click('[data-patrol-lineage] [data-context-id="ctx-12"]');
    await until('document.querySelector("[data-patrol-context-content]").textContent.includes("5 个直接来源")');
    await capture("reference-1748");
    const geometry = await run(`(()=>{const r=s=>{const {x,y,width,height}=document.querySelector(s).getBoundingClientRect();return {x,y,width,height}};return {viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,graph:r('.patrol-graph-card'),progress:r('.patrol-progress-card'),facts:r('.patrol-facts-card'),selected:r('.patrol-selected-card'),composer:r('[data-patrol-composer]')}})()`);
    fs.writeFileSync(path.join(output,"geometry.json"),JSON.stringify(geometry,null,2));
    const stable = await run(`(()=>{window.stableNode=document.querySelector('[data-context-id="ctx-12"]');window.stableForm=document.querySelector('[data-patrol-composer]');window.stableInput=document.querySelector('[data-patrol-content]');stableInput.value='选中后的工作区输入';stableInput.dispatchEvent(new Event('input'));stableInput.setSelectionRange(1,3);const map=document.querySelector('.is-workbench');return {zoom:map.dataset.zoom,position:stableNode.getAttribute('style')}})()`);
    await click('[data-patrol-related]');
    await until('document.querySelectorAll(".workbench-node[hidden]").length>0');
    await click('[data-patrol-related]');
    await until('document.querySelectorAll(".workbench-node[hidden]").length===0');
    assert.deepEqual(await run(`({zoom:document.querySelector('.is-workbench').dataset.zoom,position:stableNode.getAttribute('style')})`),stable);
    await run(`window.patrolFixture.lineage.nodes.push({context_id:'ctx-03',revision_id:'older-source',generation:0});window.patrolFixture.lineage.edges.push({source_context_id:'ctx-03',source_revision_id:'older-source',target_context_id:'ctx-12',target_revision_id:'rev-12'});window.patrolFixture.emit('loop-graph','portfolio','p2',2,{version:2})`);
    await until('document.querySelectorAll("[data-edge-hit]").length===45');
    const edgePoint = await run(`(()=>{for(const path of document.querySelectorAll('[data-edge-hit]')){const id=JSON.parse(path.dataset.edgeHit);if(id[0]!=='ctx-03'||id[2]!=='ctx-12')continue;for(let fraction=.15;fraction<.9;fraction+=.1){const local=path.getPointAtLength(path.getTotalLength()*fraction),p=new DOMPoint(local.x,local.y).matrixTransform(path.getScreenCTM());const hit=document.elementFromPoint(p.x,p.y);if(hit?.dataset.edgeHit===path.dataset.edgeHit)return {x:p.x,y:p.y};}}throw Error('No visible precise edge hit')})()`);
    pointer(edgePoint,'mouseDown',{button:'left',clickCount:1});pointer(edgePoint,'mouseUp',{button:'left',clickCount:1});
    await until('document.querySelector("[data-patrol-dialog]").open');
    assert.equal(await run('document.querySelectorAll(".workbench-edge-list li").length'),2);
    await click('[data-patrol-dialog-close]');
    await until('!document.querySelector("[data-patrol-dialog]").open');
    await click('[data-portfolio-zoom=in]');await click('[data-portfolio-zoom=in]');
    const dragPoint = await run(`(()=>{const r=document.querySelector('.portfolio-map-scroll').getBoundingClientRect();for(let y=r.bottom-35;y>r.top+30;y-=35)for(let x=r.right-35;x>r.left+140;x-=35){const hit=document.elementFromPoint(x,y);if(hit?.matches('svg,.portfolio-map-canvas,.portfolio-map-scroll'))return {x,y};}throw Error('No blank graph surface')})()`);
    pointer(dragPoint,'mouseDown',{button:'left',clickCount:1});pointer({x:dragPoint.x-110,y:dragPoint.y-60},'mouseMove',{button:'left'});pointer({x:dragPoint.x-110,y:dragPoint.y-60},'mouseUp',{button:'left',clickCount:1});
    await until('document.querySelector(".portfolio-map-scroll").scrollLeft>0');
    assert.equal(await run('document.querySelector(".workbench-node.is-selected").dataset.contextId'),"ctx-12");
    await click('[data-portfolio-zoom=fit]');
    await run(`document.querySelector('[data-context-related="ctx-03"][data-revision-id="older-source"]').focus()`);
    win.webContents.sendInputEvent({type:"keyDown",keyCode:"Enter"});win.webContents.sendInputEvent({type:"char",keyCode:"Enter"});win.webContents.sendInputEvent({type:"keyUp",keyCode:"Enter"});
    await until('document.querySelector(".workbench-version-note")');
    assert.match(await run('document.querySelector("[data-patrol-context-content]").textContent'), /历史版本/);
    assert.match(await run('document.querySelector(".workbench-node.is-selected").getAttribute("aria-label")'), /R0.*历史来源/);
    await run(`stableForm.requestSubmit()`);
    await until('window.patrolFixture.submissions.length===2');
    assert.equal(await run('window.patrolFixture.mainSubmissions.length'),0);
    assert.equal(await run('window.patrolFixture.submissions[1].content'),"选中后的工作区输入");
    await run(`window.patrolFixture.lineage.nodes.push({context_id:'ctx-03',revision_id:'new-head',generation:100});window.patrolFixture.lineage.roots['ctx-03']='new-head';window.patrolFixture.emit('loop-graph','portfolio','head-update',1,{version:100})`);
    await until('document.querySelector(\'[data-context-id="ctx-03"]\').dataset.revisionId==="new-head"');
    assert.match(await run('document.querySelector(".workbench-node.is-selected").getAttribute("aria-label")'), /R0.*历史来源/);
    await run(`delete window.patrolFixture.lineage.roots['ctx-03'];window.patrolFixture.emit('loop-graph','portfolio','external',1,{version:101})`);
    await click('[data-patrol-lineage] [data-context-id="ctx-03"]');
    await until('document.querySelector("[data-patrol-context-content]").textContent.includes("Context 不属于当前 Loop")');
    await run(`window.patrolFixture.lineage.roots['ctx-03']='new-head';window.patrolFixture.lineage.complete=false;window.patrolFixture.emit('loop-graph','portfolio','partial',1,{version:102})`);
    await until('document.querySelector("[data-patrol-lineage-error]").textContent.includes("保留上次完整结果")');
    assert.equal(await run('document.querySelectorAll(".workbench-node").length'),32);
    await run('window.patrolFixture.lineage.complete=true');
    await run(`window.patrolFixture.lineageError=true;window.patrolFixture.emit('loop-graph','portfolio','p3',3,{version:3})`);
    await until('document.querySelector("[data-patrol-lineage-error]").textContent.includes("保留上次完整结果")');
    assert.equal(await run('document.querySelectorAll(".workbench-node").length'),32);
    await run(`window.patrolFixture.lineageError=false;document.querySelector('[data-patrol-lineage-retry]').click()`);
    await until('document.querySelector("[data-patrol-lineage-error]").hidden');
    await run(`window.progressBeforeFact=document.querySelector('[data-patrol-progress] li button');progressBeforeFact.focus();window.graphChanges=[];window.graphObserver=new MutationObserver(changes=>graphChanges.push(...changes));graphObserver.observe(document.querySelector('[data-patrol-lineage]'),{subtree:true,attributes:true,childList:true,characterData:true});window.patrolFixture.emit('loop-graph','fact','fact-1',2,{kind:'run',title:'真实事实修订',status:'contradicted',outcome_status:'failed'})`);
    await until('document.querySelector("[data-patrol-facts]").textContent.includes("真实事实修订")');
    assert.equal(await run('document.querySelectorAll("[data-patrol-facts] article[data-fact-id=fact-1]").length'),1);
    assert.equal(await run('graphChanges.length'),0);
    assert.equal(await run('document.activeElement===progressBeforeFact'),true);
    await run('graphObserver.disconnect()');
    await click('[data-patrol-progress-locate="[]"]');
    await until('document.querySelector("[data-workbench-location]").textContent.includes("尚未关联")');
    await click('[data-patrol-progress-locate=\'["ctx-10","ctx-11"]\']');
    await until('document.querySelector("[data-workbench-location]").textContent.includes("2 条关联工作线")');
    await click('[data-patrol-progress-locate=\'["ctx-12"]\']');
    await until('document.querySelector(".workbench-node.is-selected").dataset.contextId==="ctx-12"');
    await run(`document.querySelector('[data-patrol-content]').focus();document.querySelector('[data-patrol-content]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}))`);
    assert.equal(await run('window.patrolFixture.submissions.length'),2);
    await win.webContents.debugger.attach('1.3');
    await run(`document.querySelector('[data-patrol-content]').value='';document.querySelector('[data-patrol-content]').focus();window.compositionEvents=[];document.querySelector('[data-patrol-content]').addEventListener('compositionstart',event=>compositionEvents.push(event.isTrusted));document.querySelector('[data-patrol-content]').addEventListener('compositionend',event=>compositionEvents.push(event.isTrusted))`);
    await win.webContents.debugger.sendCommand('Input.imeSetComposition',{text:'zhongwen',selectionStart:0,selectionEnd:8});
    win.webContents.sendInputEvent({type:'keyDown',keyCode:'Enter'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Enter'});
    await sleep(80);assert.equal(await run('window.patrolFixture.submissions.length'),2);
    await win.webContents.debugger.sendCommand('Input.insertText',{text:'中文输入'});
    const compositionEvents = await run('compositionEvents');
    assert.equal(compositionEvents[0],true);assert.equal(compositionEvents.length,2);
    assert.equal(await run('document.querySelector("[data-patrol-content]").value'),"中文输入");
    await win.webContents.debugger.detach();
    await run(`showAccessReview({task_id:'approval-graph'},{type:'access_review',tool:'read_file',cwd:'C:/isolated/graph',reads:['C:/isolated/graph/source.txt'],access_mode:'workspace'},'background-agent')`);
    assert.ok(await run(`(()=>{const panel=document.querySelector('#accessPending'),r=panel.getBoundingClientRect();return !panel.hidden&&r.top>=56&&r.bottom<innerHeight&&document.querySelector('[data-patrol-composer]').getBoundingClientRect().bottom<=innerHeight})()`));
    await capture('global-approval');
    await run('closeAccessReview()');
    for (const count of [32,128]) {
      if(count===128) await run(`window.patrolFixture.lineage={complete:true,roots:{},nodes:Array.from({length:128},(_,i)=>({context_id:'load-'+i,revision_id:'lr-'+i,generation:1})),edges:Array.from({length:127},(_,i)=>({source_context_id:'load-'+i,source_revision_id:'lr-'+i,target_context_id:'load-'+(i+1),target_revision_id:'lr-'+(i+1)}))};window.patrolFixture.emit('loop-graph','portfolio','load',128,{version:128})`);
      await until(`document.querySelectorAll('.workbench-node').length===${count}`);
      metrics.push(await run(`(()=>{const graph=document.querySelector('[data-patrol-lineage]');const data=FocusWorkspacePatrolView.lineageManifest(window.patrolFixture.lineage,{});const durations=[];for(let i=0;i<30;i++){const start=performance.now();FocusPortfolioMapView.reconcile(graph,data,null,[],{presentation:'workbench'});durations.push(performance.now()-start)}return {nodes:${count},durations}})()`));
    }
    for (const [width,height,zoom] of [[1440,900,1],[900,800,1],[390,844,1],[1280,900,2]]) {
      win.webContents.setZoomFactor(zoom);win.setContentSize(width,height);await sleep(300);
      await capture(`responsive-${width}-${zoom}`);
      assert.ok(await run('document.documentElement.scrollWidth<=innerWidth+1'));
      assert.ok(await run('(()=>{const r=document.querySelector("[data-patrol-composer]").getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})()'));
      if(width/zoom<1200) for(const panel of ['progress','observation','facts','context','graph']) {
        if(panel==='graph'&&width/zoom>=900) continue;
        await click(`[data-workbench-show="${panel}"]`);
        await until(`document.querySelector(".workspace-patrol").dataset.workbenchPanel===${JSON.stringify(panel)}`);
      }
      if(width===900){await click('[data-patrol-lineage] [data-context-id="load-0"]');await until('document.querySelector(".workspace-patrol").dataset.workbenchPanel==="context"');}
    }
    await win.webContents.debugger.attach("1.3");
    for(const feature of ['prefers-reduced-transparency','prefers-reduced-motion','forced-colors']) {
      await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia',{features:[{name:feature,value:feature==='forced-colors'?'active':'reduce'}]});
      await capture(feature);
    }
    await win.webContents.debugger.detach();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,"report.json"),JSON.stringify({fixture:true,errors,metrics,compositionEvents,checks:['empty/error/recovery','32 nodes / precise historical sources','trusted pointer exact edge / overlapping versions / graph drag','keyboard source inspection','workspace input destination','same fact revision','global approval visible','390/900/1440/200%','Chromium IME composition and Chinese commit via CDP; Windows candidate UI not covered']},null,2));
    console.log("WORKBENCH_PASS",output);win.destroy();app.quit();
  }).catch(error=>{console.error(error);app.exit(1)});
}
