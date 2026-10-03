/* 本文件对外提供真实前台 Electron 左右编排性能与编辑连续性验收。
 * 输入为 1000 entries / 约 2MiB 混合工具组、图片/文件引用与未知块 fixture；输出为三轮输入/拖动、来源跨页缓存及展开字段/raw 测量。
 * 工作流在前台 BrowserWindow 输入中文、测量下一帧反馈并连续拖动；网络时间独立，预算失败直接失败。
 * 示例：desktop/node_modules/.bin/electron desktop/patrol-workbench.e2e.cjs。
 */
const { app, BrowserWindow, screen, contentTracing } = require('electron');
const path = require('node:path'), fs = require('node:fs'), os = require('node:os');
const output = path.resolve(__dirname, '../.tmp/session-patrol-semantic-performance');
fs.mkdirSync(output, { recursive:true });
app.setPath('userData', path.join(os.tmpdir(), `focus-patrol-bench-${process.pid}`));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const p95 = values => [...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];
async function run() {
  const win = new BrowserWindow({ show:true, width:1280, height:900, webPreferences:{contextIsolation:false,nodeIntegration:false,backgroundThrottling:false} });
  await win.loadFile(path.join(__dirname,'patrol-workbench-fixture.html')); win.focus();
  await contentTracing.startRecording({included_categories:['devtools.timeline','blink.user_timing','toplevel']});
  await win.webContents.executeJavaScript(`(() => {
    window.fixtureEntries=Array.from({length:1000},(_,i)=>{
      const body='中文上下文与工具证据'.repeat(69)+'-'+i;
      const entry={entry_id:'entry-'+i,kind:'message',payload:{role:['user','assistant','developer','system'][i%4],content:body},source_ref:i%5===0?'source-'+i:null};
      if(i%10===5){entry.kind='function_call';entry.payload={call_id:'example-'+i,name:'read_file',arguments:JSON.stringify({path:'frozen-'+i+'.txt',note:body})};}
      if(i%10===6){entry.kind='function_call_output';entry.payload={call_id:'example-'+(i-1),output:body};}
      if(i%10===7){entry.payload={role:'user',content:[{type:'input_text',text:body},{type:'input_image',image_url:'data:image/png;base64,iVBORw0KGgo='},{type:'input_file',filename:'reference.txt',file_data:'data:text/plain;base64,Zm9yIHJlZmVyZW5jZQ=='},{type:'future',payload:{kept:true}}]};}
      if(i===7)entry.future_payload='长字段保真'.repeat(16000);
      return entry;
    });
    window.fixtureRequests=[]; window.longTasks=[];
    new PerformanceObserver(list=>longTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:true});
    const draft={draft_id:'benchmark',draft_revision:0,task_id:'test',equipment:{model_name:'model',permissions:['read'],skills:[]},authoring_document:{schema_version:3,entries:fixtureEntries,instructions:'基础行为',transformations:[]}};
    window.wb=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft,api:async(url,options)=>{fixtureRequests.push(url);await new Promise(r=>setTimeout(r,25));const body=JSON.parse(options?.body||'{}');return {draft_revision:(body.draft_revision||0)+1};}});
    window.fixtureSize=new TextEncoder().encode(JSON.stringify(draft.authoring_document)).length;
    window.inputSamples=[]; const editor=document.querySelector('[aria-label="条目正文"]');
    editor.addEventListener('input',()=>{const start=performance.now();requestAnimationFrame(()=>requestAnimationFrame(()=>inputSamples.push(performance.now()-start)));}); editor.focus();
  })()`);
  const rounds=[];
  for (let round=0;round<3;round++) {
    await win.webContents.executeJavaScript(`wb._locate('entry-0');inputSamples=[]; window.roundStart=performance.now(); window.initialEditor=wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]'); initialEditor.focus();`);
    for (let sample=0;sample<100;sample++) {
      win.webContents.sendInputEvent({type:'char',keyCode:'中'}); await wait(20);
    }
    await wait(60);
    const input = await win.webContents.executeJavaScript(`({samples:inputSamples, same:initialEditor===wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]'), focused:document.activeElement===initialEditor, text:initialEditor.value, cards:document.querySelectorAll('.patrol-entry').length,cache:wb.viewport.cache.size})`);
    const drag = await win.webContents.executeJavaScript(`new Promise(resolve=>{
      const start=performance.now(); const handle=document.querySelector('[aria-label="拖动排序"]'); const rect=handle.getBoundingClientRect();
      handle.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:rect.x+5,clientY:rect.y+5}));
      const samples=[]; let last=performance.now();
      const frame=()=>{const now=performance.now();samples.push(now-last);last=now;
        const list=document.querySelector('.patrol-list').getBoundingClientRect();
        document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientY:list.bottom-5,clientX:list.x+10}));
        if(samples.length<320)return requestAnimationFrame(frame);
        document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientY:list.bottom-5}));
        resolve({samples,cards:document.querySelectorAll('.patrol-entry').length,cache:wb.viewport.cache.size,duration:performance.now()-start});}; requestAnimationFrame(frame);
    })`);
    const tasks = await win.webContents.executeJavaScript(`longTasks.filter(e=>e.start>=roundStart)`);
    rounds.push({round:round+1,input_samples:input.samples.length,input_p95_ms:p95(input.samples),drag_frames:drag.samples.length,frame_p95_ms:p95(drag.samples),max_long_task_ms:Math.max(0,...tasks.map(t=>t.duration)),resident_cards:Math.max(input.cards,drag.cards),resident_cache:Math.max(input.cache,drag.cache),editor_identity:input.same,focus:input.focused,text_length:input.text.length});
    await win.webContents.executeJavaScript(`wb.list.scrollTop=0; wb.renderList(); wb.select('entry-0');`);
  }
  const expanded = await win.webContents.executeJavaScript(`(async()=>{
    const phaseStart=performance.now();
    const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    let start=performance.now();wb.select('entry-7');const details=wb.editor.querySelector('.patrol-item-structure');details.open=true;await paint();
    const fieldsOpen=performance.now()-start,fields=wb.viewport.cache.get('entry-7').structureEditor;
    const changed=JSON.parse(fields.value);changed.name='edited-large-field';fields.value=JSON.stringify(changed);fields.element.dispatchEvent(new InputEvent('input',{bubbles:true}));
    await new Promise(resolve=>setTimeout(resolve,700));await wb.flush();
    const largeKept=wb.doc.byId.get('entry-7').future_payload==='长字段保真'.repeat(16000)&&wb.doc.byId.get('entry-7').name==='edited-large-field';
    start=performance.now();const loading=wb._toggleRaw();await paint();const rawFeedback=performance.now()-start;
    const rawPending=!wb.rawState.hidden&&wb.rawEditor.readOnly;await loading;await paint();const rawReady=performance.now()-start;
    const rawNode=wb.raw,valid=wb.rawEditor.value;wb.rawEditor.setSelectionRange(2,4);rawNode.dispatchEvent(new InputEvent('input',{bubbles:true}));
    const renderedLines=rawNode.querySelectorAll('.cm-line').length;
    const identity=rawNode===wb.raw&&wb.rawEditor.hasFocus&&wb.rawEditor.selectionStart===2;
    await new Promise(resolve=>setTimeout(resolve,750));await wb.flush();
    const rawRoundtrip=wb.doc.value.entries.length===1000&&!wb.doc.value.raw_error&&wb.doc.value.raw_buffer===valid&&wb.doc.byId.get('entry-7').future_payload.length===80000;
    await wb._toggleRaw();wb.select('entry-0');return {fields_open_ms:fieldsOpen,raw_feedback_ms:rawFeedback,raw_ready_ms:rawReady,raw_pending_feedback:rawPending,large_field_preserved:largeKept,raw_rendered_lines:renderedLines,raw_input_continuity:identity,raw_roundtrip:rawRoundtrip,max_long_task_ms:Math.max(0,...longTasks.filter(e=>e.start>=phaseStart).map(e=>e.duration))};
  })()`);
  await win.webContents.executeJavaScript(`(async()=>{await wb._toggleRaw();window.rawSamples=[];window.rawPhaseStart=performance.now();window.rawIdentity=wb.raw;
    wb.raw.addEventListener('input',event=>{if(event.target!==wb.raw)return;const start=performance.now();requestAnimationFrame(()=>requestAnimationFrame(()=>rawSamples.push(performance.now()-start)));});
    wb.rawEditor.focus();wb.rawEditor.setSelectionRange(0,0);})()`);
  for(let sample=0;sample<100;sample++){win.webContents.sendInputEvent({type:'char',keyCode:' '});await wait(20);}
  await wait(700);
  const rawEditing=await win.webContents.executeJavaScript(`(async()=>{await wb.flush();const result={samples:rawSamples,editor_identity:rawIdentity===wb.raw,focus:wb.rawEditor.hasFocus,complete:wb.doc.value.entries.length===1000&&!wb.doc.value.raw_error,max_long_task_ms:Math.max(0,...longTasks.filter(e=>e.start>=rawPhaseStart).map(e=>e.duration))};await wb._toggleRaw();return result;})()`);
  const rawInput={samples:rawEditing.samples.length,input_p95_ms:p95(rawEditing.samples),editor_identity:rawEditing.editor_identity,focus:rawEditing.focus,complete:rawEditing.complete,max_long_task_ms:rawEditing.max_long_task_ms};
  const rawBefore=await win.webContents.executeJavaScript(`(async()=>{await wb._toggleRaw();wb.rawEditor.setSelectionRange(0,0);return wb.rawEditor.value;})()`);
  win.webContents.sendInputEvent({type:'char',keyCode:' '});await wait(700);
  await win.webContents.executeJavaScript(`(async()=>{await wb._toggleRaw();await wb._toggleRaw();})()`);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control']});await wait(700);
  const rawUndo=await win.webContents.executeJavaScript(`(async()=>{const same=wb.rawEditor.value===${JSON.stringify(rawBefore)};await wb._toggleRaw();return same;})()`);
  const composition=await win.webContents.executeJavaScript(`(() => {
    wb._locate('entry-0');const editor=wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]'); editor.focus(); const same=editor;
    editor.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'中'}));
    editor.value+='中文输入'; editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertCompositionText',data:'中文输入',isComposing:true}));
    const preserved=document.activeElement===same && same===wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]');
    editor.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文输入'})); return preserved;
  })()`);
  const sourceBrowsing=await win.webContents.executeJavaScript(`(async()=>{
    const originalApi=wb.api,paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))),rows=Array.from({length:1000},(_,index)=>({source_row_id:'browse-'+index,kind:'message',role:'user',eligible:true,summary:'只读来源 '+index,truncated:true}));
    wb.api=async(url,options)=>{if(!url.endsWith('/source-preview'))return originalApi(url,options);const body=JSON.parse(options.body);
      if(body.row_id)return {source_fingerprint:body.revision_id,row:{entry:{kind:'message',payload:{content:'完整只读正文'.repeat(9000)}}}};
      const offset=Number(body.cursor || 0);return {source_ref:body,source_fingerprint:body.revision_id,rows:rows.slice(offset,offset+50),total:1000,excluded_count:0,next_cursor:offset+50<1000?String(offset+50):null};};
    const c=wb.sourceController;await c.choose({kind:'context',revision_id:'R1'});await paint();c.select('browse-0',true);
    let mounted=0,cache=0;for(let i=0;i<20;i++){await paint();mounted=Math.max(mounted,wb.sourceView.list.querySelectorAll('.patrol-source-row').length);cache=Math.max(cache,wb.sourceView.viewport.cache.size);if(i<19)await c.nextPage();}
    const acrossPages=c.selected.has('browse-0');await c.choose({kind:'context',revision_id:'R2'});await paint();const cleanVersion=c.selected.size===0;
    for(let i=0;i<30;i++)await c.expand('browse-'+i);await paint();const detailCache=c.details.size,renderedLines=wb.sourceView.list.querySelectorAll('.cm-line').length;
    wb._locate('entry-0');const port=wb.viewport.cache.get('entry-0').fieldEditor('content');port.focus();port.setSelectionRange(2,4);const scroll=wb.list.scrollTop;
    await c.choose({kind:'context',revision_id:'R3'});await paint();const continuity=document.activeElement===port&&port.selectionStart===2&&wb.list.scrollTop===scroll;
    wb.list.scrollTop=10000;wb.viewport.render();await paint();const index=wb.viewport.indexAt(wb.list.scrollTop),id=wb.viewport.renderedKeys[index],within=wb.list.scrollTop-wb.viewport.offsets[index];
    wb.doc.insert({entry_id:'anchor-test',kind:'message',payload:{role:'user',content:'above viewport'}},0);wb.renderList();await paint();const next=wb.doc.value.entries.findIndex(entry=>entry.entry_id===id),anchored=Math.abs(wb.list.scrollTop-wb.viewport.offsets[next]-within)<2;wb.doc.remove('anchor-test');wb.renderList();
    wb.api=originalApi;return {rows:1000,pages:20,mounted_max:mounted,cache_max:cache,detail_cache:detailCache,full_detail_rendered_lines:renderedLines,cross_page_selection:acrossPages,switch_version_clears_selection:cleanVersion,source_keeps_target_focus_and_scroll:continuity,structural_scroll_anchor:anchored};
  })()`);
  const interactions = await win.webContents.executeJavaScript(`(async () => {
    const assertions={};wb._locate('entry-0'); const editor=wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]'); editor.focus(); editor.setSelectionRange(2,4);
    const identity=editor; await wb.flush(); assertions.save_keeps_editor=identity===wb.cards.get('entry-0').querySelector('[aria-label="条目正文"]')&&document.activeElement===identity&&identity.selectionStart===2&&identity.selectionEnd===4;
    editor.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'续'}));editor.value+='导航保留中文';editor.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));
    wb.leave();FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft:wb.draft,api:wb.api});await wb.flush();
    assertions.composition_navigation=!wb.composing&&!wb.queue.held&&wb.doc.byId.get('entry-0').payload.content.includes('导航保留中文');
    const before=wb.doc.value.entries.length,startFeedback=performance.now(); wb.doc.clear(); wb.renderList(); wb._undo(false); assertions.clear_undo=wb.doc.value.entries.length===before;
    window.localFeedbackMs=performance.now()-startFeedback;assertions.local_feedback_budget=localFeedbackMs<=100;
    await wb._toggleRaw();const rawNode=wb.raw;wb.rawEditor.setSelectionRange(10,20);wb.list.scrollTop=40000;wb.renderList();
    assertions.raw_editor_pin=rawNode===wb.raw&&wb.rawEditor.hasFocus&&wb.rawEditor.selectionStart===10&&getComputedStyle(wb.list).display==='none';await wb._toggleRaw();
    const old=wb; let confirm; old.queue.save=()=>new Promise(resolve=>{confirm=resolve;}); old.doc.setPayloadField('entry-0','content','旧草稿未保存'); const saving=old.flush();
    await Promise.resolve();
    const draft={draft_id:'other',draft_revision:0,task_id:'other',equipment:{model_name:'model',permissions:['read'],skills:[]},authoring_document:{schema_version:2,instructions:'',entries:[{entry_id:'new',role:'user',content:'新草稿'}],transformations:[]}};
    const current=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft,api:async(_url,options)=>({draft_revision:(JSON.parse(options?.body||'{}').draft_revision||0)+1})});
    const currentEditor=current.editor.querySelector('[aria-label="条目正文"]');currentEditor.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
    assertions.navigation_listener_cleanup=current.composing&&!old.composing;currentEditor.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));
    current.doc.setPayloadField('new','content','新草稿继续输入'); confirm({draft_revision:old.queue.serverRevision+1}); await saving;
    assertions.cross_draft_late_save=current.doc.byId.get('new').payload.content==='新草稿继续输入'&&old.doc.byId.get('entry-0').payload.content==='旧草稿未保存';
    let previewDone; current.api=async(url,options)=>url.endsWith('/preview')?new Promise(resolve=>{previewDone=resolve;}):({draft_revision:JSON.parse(options?.body||'{}').draft_revision+1});
    const pending=current.preview(); while(!previewDone)await new Promise(resolve=>setTimeout(resolve,5)); current.draft.equipment.model_name='new-model';current.configurationChanged();
    const late=previewDone;current.api=async(url,options)=>url.endsWith('/preview')?({executable:true,diagnostics:[],preview_token:'fresh-model'}):({draft_revision:JSON.parse(options?.body||'{}').draft_revision+1});await current.preview();
    late({executable:true,diagnostics:[],preview_token:'stale'});await pending;assertions.stale_preview=current.previewPlan.preview_token==='fresh-model';
    await current._toggleRaw(); current.rawEditor.value='{ invalid JSON 中文'; current.raw.dispatchEvent(new InputEvent('input',{bubbles:true})); await new Promise(resolve=>setTimeout(resolve,650));
    assertions.invalid_raw_preserved=current.doc.value.raw_buffer==='{ invalid JSON 中文'&&Boolean(current.doc.value.raw_error);
    await current._toggleRaw(); const cards=document.querySelectorAll('.patrol-entry').length; assertions.no_truncation=current.doc.value.entries.length===1&&cards<=120;
    current.doc.insert({entry_id:'keyboard',kind:'message',payload:{role:'user',content:'键盘条目'}});current.select('new');
    const active=current.editor.querySelector('[aria-label="条目正文"]');active.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'ArrowDown',altKey:true}));
    assertions.keyboard_reorder=current.doc.value.entries[1].entry_id==='new';
    const card=current.cards.get('new');card.focus();card.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'z',ctrlKey:true}));assertions.keyboard_undo=current.doc.value.entries[0].entry_id==='new';
    const order=current.doc.value.entries.map(e=>e.entry_id).join(',');const handle=current.cards.get('new').querySelector('[aria-label="拖动排序"]');const bounds=handle.getBoundingClientRect();
    handle.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:bounds.x+2,clientY:bounds.y+2}));current.root.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Escape'}));
    assertions.drag_cancel=!current.drag&&!current._cancelDrag&&current.doc.value.entries.map(e=>e.entry_id).join(',')===order;
    let sourceDone;current.api=async(url,options)=>url.includes('/patrol/sources')?new Promise(resolve=>{sourceDone=resolve;}):url.endsWith('/preview')?({executable:true,diagnostics:[],preview_token:'newer'}):({draft_revision:JSON.parse(options?.body||'{}').draft_revision+1});
    const oldSource=current.sourceController.directory();while(!sourceDone)await new Promise(resolve=>setTimeout(resolve,5));await current.preview();sourceDone({items:[],next_cursor:null});await oldSource;
    assertions.source_out_of_order=current.previewPlan.preview_token==='newer'&&Boolean(current.panel.querySelector('details'));
    localStorage.setItem('focus-patrol-draft:recovery',JSON.stringify({serverRevision:3,document:{schema_version:2,entries:[{entry_id:'local',role:'user',content:'本地未确认正文'}]}}));
    const remote={draft_id:'recovery',draft_revision:7,equipment:{},authoring_document:{schema_version:2,entries:[{entry_id:'server',role:'user',content:'服务器另一窗口正文'}]}};
    const recovery=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft:remote,api:async(_url,options)=>{if(options?.method==='PUT')throw Error('409 revision conflict');return remote;}});
    assertions.local_conflict_recovery=recovery.localConflict&&recovery.queue.serverRevision===3&&recovery.doc.byId.get('local').payload.content==='本地未确认正文';
    await recovery.queue.flush().catch(()=>{});await recovery._reloadServer();
    assertions.explicit_server_reload=recovery.queue.serverRevision===7&&recovery.doc.byId.get('server').payload.content==='服务器另一窗口正文';recovery._undo(false);
    assertions.server_reload_undo=recovery.doc.byId.get('local').payload.content==='本地未确认正文';clearTimeout(recovery.queue.timer);
    return assertions;
  })()`);
  interactions.raw_native_undo_after_view_toggle=rawUndo;
  const nativeBefore=await win.webContents.executeJavaScript(`(() => {const editor=document.querySelector('[aria-label="条目正文"]');editor.focus();editor.setSelectionRange(editor.value.length,editor.value.length);return editor.value;})()`);
  win.webContents.sendInputEvent({type:'char',keyCode:'撤'});await wait(30);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control']});await wait(30);
  interactions.native_text_undo=await win.webContents.executeJavaScript(`document.querySelector('[aria-label="条目正文"]').value===${JSON.stringify(nativeBefore)}`);
  win.webContents.debugger.attach('1.3');
  await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  interactions.reduced_motion=await win.webContents.executeJavaScript(`matchMedia('(prefers-reduced-motion:reduce)').matches&&getComputedStyle(document.querySelector('.patrol-entry')).transitionDuration==='0s'`);
  win.webContents.debugger.detach();
  const trace=await contentTracing.stopRecording(path.join(output,'electron-trace.json'));
  const display=screen.getPrimaryDisplay();
  const fixtureContent=await win.webContents.executeJavaScript(`({entries:fixtureEntries.length,tool_calls:fixtureEntries.filter(e=>e.kind==='function_call').length,tool_outputs:fixtureEntries.filter(e=>e.kind==='function_call_output').length,multimodal_entries:fixtureEntries.filter(e=>Array.isArray(e.payload.content)).length,large_field_bytes:new TextEncoder().encode(fixtureEntries.find(e=>e.entry_id==='entry-7').future_payload).length})`);
  const report={hardware:{cpu:os.cpus()[0].model,logical_cpus:os.cpus().length,memory_bytes:os.totalmem(),platform:os.platform(),electron:process.versions.electron,chrome:process.versions.chrome,refresh_rate_hz:display.displayFrequency,viewport:{width:1280,height:900}},fixture_bytes:await win.webContents.executeJavaScript('fixtureSize'),fixture_content:fixtureContent,rounds,expanded,raw_input:rawInput,interactions,local_feedback_ms:await win.webContents.executeJavaScript('localFeedbackMs'),composition_event_continuity:composition,trace,notes:['输入使用 Electron sendInputEvent，composition 使用浏览器事件；未覆盖物理中文 IME 候选窗口。','网络 mock 延迟 25ms 不计入 paint；未调用真实 Provider。','展开字段/raw 测量从本地操作到 double RAF；大型解析独立在 Worker 执行，反馈与全文就绪耗时分列。']};
  report.source_browsing=sourceBrowsing;
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));
  const passed=rounds.every(r=>r.input_samples>=100&&r.drag_frames>=300&&r.input_p95_ms<=50&&r.frame_p95_ms<=20&&r.max_long_task_ms<=100&&r.resident_cards<=120&&r.editor_identity&&r.focus)&&composition&&Object.values(interactions).every(Boolean)&&expanded.fields_open_ms<=100&&expanded.raw_feedback_ms<=100&&expanded.raw_pending_feedback&&expanded.large_field_preserved&&expanded.raw_rendered_lines<=120&&expanded.raw_input_continuity&&expanded.raw_roundtrip&&expanded.max_long_task_ms<=100&&rawInput.samples>=100&&rawInput.input_p95_ms<=50&&rawInput.editor_identity&&rawInput.focus&&rawInput.complete&&rawInput.max_long_task_ms<=100;
  const sourcePassed=sourceBrowsing.mounted_max<=80&&sourceBrowsing.cache_max<=120&&sourceBrowsing.detail_cache<=24&&sourceBrowsing.cross_page_selection&&sourceBrowsing.switch_version_clears_selection&&sourceBrowsing.source_keeps_target_focus_and_scroll&&sourceBrowsing.structural_scroll_anchor;
  const targetPassed=rounds.every(round=>round.resident_cards<=80&&round.resident_cache<=120);
  win.destroy();app.exit(passed&&sourcePassed&&targetPassed?0:1);
}
app.whenReady().then(run).catch(error=>{console.error(error.stack);app.exit(1);});
