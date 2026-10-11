/* 本文件对外提供全图玻璃关系区域的生产页面交互与视觉验收。
 * 输入为既有隔离 API fixture、图拓扑和窗口尺寸；输出为真实指针/键盘、版本检查、原业务分流、错误恢复与截图证据。
 * 工作流为通过 Patrol 创建测试绑定后进入全图，沿真实页面查询和检查入口操作；隔离数据不代表生产业务，真实 HTTP 另由 migration-real 验收。
 * 示例：node desktop/global-map-graph.e2e.cjs；FOCUS_MAP_EVIDENCE 可指定证据目录。
 */
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), os = require("node:os");
if (!process.versions.electron) {
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"),[__filename],{env,encoding:"utf8",windowsHide:true,timeout:120000});
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const {app,BrowserWindow}=require("electron");
  app.setPath("userData",fs.mkdtempSync(path.join(os.tmpdir(),"focus-global-map-")));
  const output=process.env.FOCUS_MAP_EVIDENCE || path.join(__dirname,"../openspec/changes/refactor-global-map-glass-graph/evidence/after");
  fs.mkdirSync(output,{recursive:true});
  app.whenReady().then(async()=>{
    const win=new BrowserWindow({show:false,width:1748,height:1106,webPreferences:{preload:path.join(__dirname,"workspace-patrol-test-preload.cjs"),contextIsolation:false,sandbox:false,backgroundThrottling:false,offscreen:true}});
    win.setContentSize(1748,1106);
    const errors=[], checks=[], timings=[];
    win.webContents.on("console-message",(_event,_level,message)=>{if(message?.includes("Uncaught"))errors.push(message)});
    const run=async code=>{try{return await win.webContents.executeJavaScript(code,true)}catch(error){throw new Error(code+": "+error.message)}};
    const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const until=async code=>{const end=Date.now()+10000;while(!await run(`Boolean(${code})`)){if(Date.now()>end)throw new Error("Timeout "+code);await sleep(20)}};
    const pointer=(point,type,extra={})=>win.webContents.sendInputEvent({type,x:Math.round(point.x*win.webContents.getZoomFactor()),y:Math.round(point.y*win.webContents.getZoomFactor()),...extra});
    const click=async selector=>{const point=await run(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});n.scrollIntoView({block:'nearest',inline:'nearest'});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);pointer(point,"mouseDown",{button:"left",clickCount:1});pointer(point,"mouseUp",{button:"left",clickCount:1});await sleep(40)};
    const capture=async(name,selector)=>{await run(`Promise.all(document.getAnimations().filter(a=>Number.isFinite(a.effect.getComputedTiming().endTime)).map(a=>a.finished.catch(()=>{})))`);await sleep(100);const rect=selector?await run(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}})()`):undefined;const pixels=await win.webContents.capturePage(rect);fs.writeFileSync(path.join(output,name+".png"),pixels.resize({width:rect?.width || Math.round(await run('innerWidth'))}).toPNG())};
    await win.loadFile(path.join(__dirname,"index.html"));await until('document.querySelector("[data-patrol-content]")');
    await run(`state.patrolWorkspace={workspace_id:'map',display_name:'looptest',path:'C:/isolated/map'};render()`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='全图验收';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('window.patrolFixture.connected("loop-map")');await click('[data-action=show-map]');
    await until('document.querySelector(".loop-map-empty") && !mapPortfolio.loading');await capture("empty");
    await run(`window.patrolFixture.lineageError=true`);await click('[data-action=refresh-map-portfolio]');
    await until('document.querySelector("[data-map-status]").textContent.includes("读取失败")');await capture("error");
    await run(`window.patrolFixture.lineageError=false;window.patrolFixture.graphConversations=true;window.patrolFixture.lineage={complete:true,roots:{root:'r1'},nodes:[{context_id:'root',revision_id:'r1',generation:1}],edges:[]};window.patrolFixture.mapConsole={loop_id:'loop-map',nodes:[{context_id:'root',title:'首条工作线',status:'active',latest_run:{run_id:'run-root',status:'running'}}]}`);
    await click('[data-action=refresh-map-portfolio]');
    await run(`patrolFixture.emit('loop-map','context','root',1,{title:'首条工作线',status:'active',current_revision_id:'r1'});patrolFixture.emit('loop-map','run','run-root',1,{context_id:'root',status:'running'})`);
    await until('document.querySelector(".workbench-node .is-running")');await sleep(80);
    const centered=await run(`(()=>{const a=document.querySelector('[data-context-id=root]').getBoundingClientRect(),b=document.querySelector('.portfolio-map-scroll').getBoundingClientRect();return {dx:Math.abs(a.x+a.width/2-b.x-b.width/2),dy:Math.abs(a.y+a.height/2-b.y-b.height/2)}})()`);
    assert.ok(centered.dx<12 && centered.dy<12,JSON.stringify(centered));await capture("single");
    await click('[data-action=toggle-selection-mode]');await click('[data-context-id=root]');
    assert.deepEqual(await run('[...state.selectedContextIds]'),['root']);assert.equal(await run('document.querySelector("[data-patrol-context]").hidden'),true);
    await run(`window.patrolFixture.mapDeletes=[];window.mapConfirmations=[];window.confirm=message=>{mapConfirmations.push(message);return false};void 0`);
    await click('[data-action=batch-delete-selected]');assert.equal(await run('patrolFixture.mapDeletes.length'),0);
    await click('[data-action=batch-cascade-delete-selected]');assert.match(await run('mapConfirmations.at(-1)'),/派生后代/);
    await run(`window.confirm=message=>{mapConfirmations.push(message);return true};patrolFixture.deleteError=true`);
    await click('[data-action=batch-delete-selected]');await until('document.querySelector("#statusText")?.textContent.includes("删除被拒绝") || patrolFixture.mapDeletes.length===1');
    assert.deepEqual(await run('patrolFixture.mapDeletes[0]'),{context_ids:['root'],cascade:false});assert.equal(await run('state.selectionMode'),true);
    await click('[data-action=toggle-selection-mode]');await click('[data-action=arm-soldier]');
    await run(`document.querySelector('[data-context-id=root]').focus()`);win.webContents.sendInputEvent({type:"keyDown",keyCode:"Enter"});win.webContents.sendInputEvent({type:"char",keyCode:"Enter"});win.webContents.sendInputEvent({type:"keyUp",keyCode:"Enter"});
    await until('document.body.dataset.view === "draft"');assert.deepEqual(await run('window.patrolFixture.draftOpens'),['root']);
    await run(`state.soldierArmed=false;state.view='map';render()`);await until('document.querySelector("[data-map-graph]")');
    await click('[data-action=toggle-selection-mode]');await click('[data-context-id=root]');await run('patrolFixture.deleteError=false');await click('[data-action=batch-cascade-delete-selected]');
    await until('!mapPortfolio.loading && document.querySelector(".loop-map-empty")');assert.deepEqual(await run('patrolFixture.mapDeletes.at(-1)'),{context_ids:['root'],cascade:true});
    await click('[data-map-view=tree]');await until('document.querySelector(".map-tree-host")');await click('[data-action=collapse-map-tree]');
    await click('[data-action=toggle-map-workspace]');assert.ok(await run('document.querySelectorAll("[data-action=toggle-map-context]").length>0'));
    await click('[data-map-view=cards]');assert.ok(await run('document.querySelectorAll("[data-action=archive-context]").length>0'));assert.ok(await run('document.querySelectorAll("[data-action=cascade-delete-context]").length>0'));
    await click('[data-map-view=graph]');await until('document.querySelector("[data-map-graph]")');
    const names = ["需求澄清","执行边界","任务规格","API 调研","可行性实验","工作区权限","验收标准","架构探索","边界情形","工具与执行","工具桥接","桌宠交互","窗口行为","动效验证","快捷键方案","视图状态","异步队列","存储适配","生命周期","恢复策略","会话持久化","端到端回归","键盘导航测试","工具契约测试","测试夹具","视觉快照","异常恢复测试","构建与打包","版本检查","集成验证","发布审查","使用文档"];
    const nodes = names.map((title, index) => ({context_id:`ctx-${String(index + 1).padStart(2,"0")}`,revision_id:`rev-${index + 1}`,generation:index % 9 + 1,title}));
    const pairs = [[0,2],[1,2],[2,7],[2,11],[3,9],[4,9],[5,9],[6,11],[7,11],[8,11],[9,10],[10,11],[11,12],[11,13],[11,14],[11,21],[12,29],[13,29],[14,29],[15,21],[16,18],[17,18],[18,20],[19,20],[20,21],[21,23],[22,21],[23,29],[24,21],[25,29],[26,29],[27,29],[28,29],[29,30],[29,31],[1,9],[6,29],[2,18],[3,20],[10,24],[12,25],[18,27],[20,26],[23,28]];
    const lineage = {complete:true, roots:Object.fromEntries(nodes.map(node=>[node.context_id,node.revision_id])),nodes,edges:pairs.map(([a,b])=>({source_context_id:nodes[a].context_id,source_revision_id:nodes[a].revision_id,target_context_id:nodes[b].context_id,target_revision_id:nodes[b].revision_id}))};

    await run(`window.patrolFixture.lineage=${JSON.stringify(lineage)};window.patrolFixture.mapConsole={loop_id:'loop-map',nodes:${JSON.stringify(nodes.map((node,i)=>({...node,title:node.title,status:'active',latest_run:i===11?{run_id:'run-12',status:'running'}:null})))}}`);
    await click('[data-action=refresh-map-portfolio]');await until('document.querySelectorAll(".workbench-node").length===32');
    await capture("reference-full");await capture("reference-graph",".map-portfolio-surface");
    const original=await run(`(()=>{window.mapNode=document.querySelector('[data-context-id="ctx-12"]');window.mapCanvas=document.querySelector('.portfolio-map-canvas');window.mapInput=document.querySelector('[data-page-search=map]');return {position:mapNode.getAttribute('style'),task:state.activeTaskId}})()`);
    await click('[data-context-id="ctx-12"]');await until('document.querySelector("[data-patrol-context-content]").textContent.includes("rev-12")');
    assert.equal(await run('state.activeTaskId'),original.task);assert.equal(await run('document.body.dataset.view'),'map');
    await capture("selected");
    const region=await run(`(()=>{const r=document.querySelector('.map-portfolio-surface').getBoundingClientRect();return {width:r.width,height:r.height}})()`);
    const [windowWidth,windowHeight]=win.getContentSize();
    win.setContentSize(Math.round(windowWidth+1258-region.width),Math.round(windowHeight+826-region.height));await sleep(100);await click('[data-portfolio-zoom=fit]');
    await capture('selected-reference',' .map-portfolio-surface');
    const geometry=await run(`(()=>{const r=document.querySelector('.map-portfolio-surface').getBoundingClientRect();return {viewport:[innerWidth,innerHeight],density:devicePixelRatio,graph:[r.width,r.height],zoom:document.querySelector('.portfolio-map').dataset.zoom}})()`);
    win.setContentSize(1748,1106);await sleep(100);
    await run(`mapInput.value='桌宠';mapInput.dispatchEvent(new Event('input',{bubbles:true}))`);
    assert.equal(await run('mapInput===document.querySelector("[data-page-search=map]") && mapNode===document.querySelector("[data-context-id=ctx-12]")'),true);
    assert.equal(await run('mapNode.getAttribute("style")'),original.position);assert.ok(await run('document.querySelectorAll(".is-search-muted").length>0'));
    await run(`mapInput.value='';mapInput.dispatchEvent(new Event('input',{bubbles:true}))`);
    await click('[data-action=map-related]');assert.ok(await run('document.querySelectorAll(".workbench-node[hidden]").length>0'));
    await click('[data-action=map-related]');
    await run(`window.patrolFixture.lineage.nodes.push({context_id:'ctx-03',revision_id:'r0',generation:0});window.patrolFixture.lineage.edges.push({source_context_id:'ctx-03',source_revision_id:'r0',target_context_id:'ctx-12',target_revision_id:'rev-12'})`);
    await click('[data-action=refresh-map-portfolio]');await until('document.querySelectorAll("[data-edge-hit]").length===45');
    assert.equal(await run('mapCanvas===document.querySelector(".portfolio-map-canvas")'),true);
    const edgePoint=await run(`(()=>{for(const p of document.querySelectorAll('[data-edge-hit]')){const id=JSON.parse(p.dataset.edgeHit);if(id[0]!=='ctx-03'||id[2]!=='ctx-12')continue;for(let f=.1;f<.95;f+=.1){const v=p.getPointAtLength(p.getTotalLength()*f),q=new DOMPoint(v.x,v.y).matrixTransform(p.getScreenCTM());if(document.elementFromPoint(q.x,q.y)?.dataset.edgeHit===p.dataset.edgeHit)return {x:q.x,y:q.y}}}throw Error('No visible edge')})()`);
    pointer(edgePoint,'mouseDown',{button:'left',clickCount:1});pointer(edgePoint,'mouseUp',{button:'left',clickCount:1});
    await until('document.querySelector(".map-relationship-dialog")?.open');assert.equal(await run('document.querySelectorAll(".workbench-edge-list li").length'),2);
    await click('.map-relationship-dialog [data-revision-id="r0"]');await until('mapContextInspector.selection()?.revisionId === "r0"');
    await run(`window.patrolFixture.lineage.nodes.push({context_id:'ctx-03',revision_id:'r99',generation:99});window.patrolFixture.lineage.roots['ctx-03']='r99'`);
    await click('[data-action=refresh-map-portfolio]');await until('!mapPortfolio.loading');assert.equal(await run('mapContextInspector.selection().revisionId'),'r0');
    await click('[data-patrol-context-tab=sources]');await until('document.querySelector("[data-patrol-inspect-revision]")');
    await run(`document.querySelector('[data-patrol-inspect-revision="r99"]').focus()`);win.webContents.sendInputEvent({type:'keyDown',keyCode:'Enter'});win.webContents.sendInputEvent({type:'char',keyCode:'Enter'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Enter'});
    await until('mapContextInspector.selection()?.revisionId === "r99"');
    await run(`window.patrolFixture.lineage.complete=false`);await click('[data-action=refresh-map-portfolio]');await until('document.querySelector("[data-map-status]").textContent.includes("未更新")');assert.equal(await run('document.querySelectorAll(".workbench-node").length'),32);
    await run(`window.patrolFixture.lineage.complete=true`);await click('[data-action=refresh-map-portfolio]');await until('!mapPortfolio.loading');
    await click('[data-patrol-context-close]');await until('document.querySelector("[data-patrol-context]").hidden');
    await click('[data-portfolio-zoom=in]');await click('[data-portfolio-zoom=in]');
    const point=await run(`(()=>{const r=document.querySelector('.portfolio-map-scroll').getBoundingClientRect();for(let y=r.bottom-25;y>r.top+20;y-=30)for(let x=r.right-25;x>r.left+100;x-=40){if(document.elementFromPoint(x,y)?.matches('svg,.portfolio-map-canvas,.portfolio-map-scroll'))return {x,y}}throw Error('No blank canvas')})()`);
    pointer(point,'mouseDown',{button:'left',clickCount:1});pointer({x:point.x-130,y:point.y-85},'mouseMove',{button:'left'});pointer({x:point.x-130,y:point.y-85},'mouseUp',{button:'left',clickCount:1});
    await sleep(60);
    const viewport=await run(`({zoom:document.querySelector('.portfolio-map').dataset.zoom,x:document.querySelector('.portfolio-map-scroll').scrollLeft,y:document.querySelector('.portfolio-map-scroll').scrollTop})`);
    assert.ok(viewport.x>0 || viewport.y>0);
    await click('[data-action=refresh-map-portfolio]');await until('!mapPortfolio.loading');assert.deepEqual(await run(`({zoom:document.querySelector('.portfolio-map').dataset.zoom,x:document.querySelector('.portfolio-map-scroll').scrollLeft,y:document.querySelector('.portfolio-map-scroll').scrollTop})`),viewport);
    for(const [width,height,zoom] of [[1440,900,1],[900,800,1],[390,844,1],[1280,900,2]]){
      win.setContentSize(width,height);win.webContents.setZoomFactor(zoom);await sleep(120);await capture(`responsive-${width}-${zoom}`);
      const bounds=await run(`(()=>{const r=document.querySelector('.map-portfolio-surface').getBoundingClientRect(),z=document.querySelector('.portfolio-map-zoom').getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth+2,inside:z.left>=r.left-1&&z.right<=r.right+1&&z.bottom<=r.bottom+1}})()`);
      assert.equal(bounds.overflow,false,JSON.stringify({width,zoom,bounds}));assert.equal(bounds.inside,true,JSON.stringify({width,zoom,bounds}));
    }
    win.webContents.setZoomFactor(1);win.setContentSize(1748,1106);await sleep(100);
    win.webContents.debugger.attach('1.3');
    for(const feature of ['forced-colors','prefers-reduced-transparency']){await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia',{features:[{name:feature,value:feature==='forced-colors'?'active':'reduce'}]});await capture(feature)}
    await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia',{features:[]});win.webContents.debugger.detach();
    await run(`delete patrolFixture.lineage.roots['ctx-03']`);await click('[data-action=refresh-map-portfolio]');await until('!mapPortfolio.loading');
    await click('[data-context-id=ctx-03]');await until('document.querySelector("[data-patrol-context-content]").textContent.includes("Context 不属于当前 Loop")');
    assert.equal(await run('document.body.dataset.view'),'map');await capture('permission-error');
    await run(`document.querySelector('[data-patrol-context-close]').focus()`);
    win.webContents.sendInputEvent({type:'keyDown',keyCode:'Tab'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Tab'});await sleep(40);
    assert.equal(await run('document.activeElement.dataset.patrolContextTab'),'overview');
    win.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'});
    await until('document.querySelector("[data-patrol-context]").hidden');assert.equal(await run('document.activeElement.dataset.contextId'),'ctx-03');
    const largeNodes=Array.from({length:128},(_,i)=>({context_id:`stress-${i}`,revision_id:`v-${i}`,generation:1,title:`工作线 ${i}`}));
    const largeGraph={complete:true,roots:Object.fromEntries(largeNodes.map(n=>[n.context_id,n.revision_id])),nodes:largeNodes,edges:largeNodes.slice(1).map((n,i)=>({source_context_id:largeNodes[i].context_id,source_revision_id:largeNodes[i].revision_id,target_context_id:n.context_id,target_revision_id:n.revision_id}))};
    await run(`window.patrolFixture.lineage=${JSON.stringify(largeGraph)};window.patrolFixture.mapConsole={loop_id:'loop-map',nodes:${JSON.stringify(largeNodes)}}`);
    await click('[data-action=refresh-map-portfolio]');await until('document.querySelectorAll(".workbench-node").length===128');
    timings.push(await run(`(()=>{const node=document.querySelector('.workbench-node'),position=node.getAttribute('style'),start=performance.now();for(let i=0;i<20;i++){mapPortfolio.manifest.nodes[0].latest_run={status:i%2?'running':'failed'};patchMapGraph()}return {nodes:128,updates:20,elapsedMs:performance.now()-start,stable:node===document.querySelector('.workbench-node')&&position===node.getAttribute('style')}})()`));
    assert.equal(timings[0].stable,true);assert.ok(timings[0].elapsedMs<3000,JSON.stringify(timings));await capture('stress-128');
    await run(`window.patrolFixture.holdLineage=true;void hydrateMapPortfolio('map',true)`);await until('window.patrolFixture.releaseLineage');
    await capture('loading');
    await run(`state.patrolWorkspace={workspace_id:'other',display_name:'other'};render()`);await until('mapPortfolio.workspaceId === "other" && !mapPortfolio.loading');
    await run('window.patrolFixture.releaseLineage()');await sleep(100);assert.equal(await run('document.querySelectorAll(".workbench-node").length'),0);
    checks.push('empty/single/32/128','console Run preservation','batch cancel/delete conflict/cascade refresh and keyboard equipment','tree/card lifecycle entries','search and focus preservation','exact edges and historical pin','partial snapshot retry','pointer drag and viewport retention','responsive/preferences','permission error and Escape focus return','workspace late response isolation');
    assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({fixture:true,errors,checks,centered,geometry,timings},null,2));console.log('PASS global map graph');win.destroy();app.quit();
  }).catch(error=>{console.error(error);app.exit(1)});
}
