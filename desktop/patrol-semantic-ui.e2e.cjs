/* 本文件提供 typed Patrol 连续卡片的真实 Electron 视觉与交互验收。
 * 输入为生产模块、混合 Focus 语义和可控异步 API；输出为八种截图及身份/正文/IME/结构/请求断言。
 * 工作流从 UI 新建五类核心项，编辑完整语义 JSON，检查独立只读请求；实际 API/图由对应集成验收提供。
 * 示例：electron desktop/patrol-semantic-ui.e2e.cjs。
 */
const {app,BrowserWindow}=require('electron');
const path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const out=path.resolve(__dirname,'../.tmp/session-patrol-semantic-ui');fs.mkdirSync(out,{recursive:true});
app.setPath('userData',path.join(os.tmpdir(),'focus-patrol-semantic-'+process.pid));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function run(){
 const win=new BrowserWindow({show:true,width:1280,height:940,webPreferences:{contextIsolation:false,nodeIntegration:false,backgroundThrottling:false}});
 win.webContents.on('console-message',(_,level,message)=>{if(level>=2)console.error(message);});
 await win.loadFile(path.join(__dirname,'patrol-workbench-fixture.html'));win.focus();
 async function shot(name){await wait(120);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());}
 await win.webContents.executeJavaScript(String.raw`(()=>{
  localStorage.clear();window.assertions={};window.assert=(value,label)=>{if(!value)throw Error(label);assertions[label]=true;};
  window.wait=ms=>new Promise(r=>setTimeout(r,ms));
  const draft={draft_id:'semantic',draft_revision:0,task_id:'task',equipment:{},authoring_document:{schema_version:3,instructions:'检查问题、验证事实并给出结论。',entries:[],transformations:[]}};
  window.wb=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft,api:async(url,options)=>{
   if(url.endsWith('/source-status'))return {sources:{ref:{status:'unavailable',message:'原文件已删除'}}};
   if(url.endsWith('/preview'))return {executable:true,diagnostics:[],budget:{total:800,context_window:64000,input:200,tools:50,instructions:50,output_reserve:500},request:{instructions:wb.doc.value.instructions,input:wb.doc.value.entries.map(e=>({type:e.kind==='message'?'message':e.kind,...e.payload})),tools:[]},role_mappings:[],items:[]};
   if(url.endsWith('/sources')&&!options)return {contexts:[{title:'架构讨论',context_id:'task',revisions:[{revision_id:'R1',generation:3}]}],branches:[]};
   if(url.includes('/materials'))return [];
   return {draft_revision:JSON.parse(options?.body || '{}').draft_revision+1};}});
 })()`);
 await shot('01-empty');
 await win.webContents.executeJavaScript(String.raw`(()=>{
  const add=(kind,role,content)=>{wb._add(kind,role);const e=wb.doc.value.entries.at(-1);if(content)wb.doc.setPayloadField(e.entry_id,'content',content);return e;};
  const d=add('message','developer','你负责检查这个问题：区分任务事实与运行状态，核对引用来源。');
  const u=add('message','user','帮我分析 Focus 的上下文编译链路，看看工具调用是否独立保留。');u.source_ref='ref';u.source_hash='frozen';u.source_item_id='original-user-item';
  add('message','assistant','我会检查语义 Item、编译结果与实际请求之间的映射。');
  const c=add('function_call');c.payload={name:'read_file',call_id:'read-architecture',arguments:'{\n  "path": "backend/app/desktop/session_patrol/compiler.py"\n}'};
  const o=add('function_call_output');o.payload={call_id:'read-architecture',output:'读取完成：编译器直接生成 typed FocusItems，兼容消息仅作为下游 bridge。'};
  wb.renderList();wb.list.scrollTop=0;
  assert(wb.doc.value.entries.every(e=>e.kind&&e.payload),'typed authoring document');
  assert(wb.doc.value.entries[3].kind==='function_call'&&wb.doc.value.entries[4].kind==='function_call_output','independent calls and outputs');
  assert(document.querySelectorAll('.patrol-entry').length===5,'continuous five core cards');
  const sourceCard=wb.cards.get(u.entry_id);assert(sourceCard.textContent.includes('冻结内容保留'),'unavailable source has readable status');
 })()`);
 await shot('02-mixed-items');
 await win.webContents.executeJavaScript(String.raw`wb._locate(wb.doc.value.entries[3].entry_id)`);await shot('08-tool-group');
 await win.webContents.executeJavaScript(String.raw`wb.list.scrollTop=0;wb.viewport.schedule()`);
 const result=await win.webContents.executeJavaScript(String.raw`(async()=>{
  const e=wb.doc.value.entries[1],card=wb.cards.get(e.entry_id),body=card.querySelector('[aria-label="条目正文"]');body.focus();body.value+=' 用户编辑';body.dispatchEvent(new InputEvent('input',{bubbles:true}));body.setSelectionRange(2,4);await wb.flush();
  assert(wb.cards.get(e.entry_id)===card&&card.querySelector('[aria-label="条目正文"]')===body&&document.activeElement===body&&body.selectionStart===2,'save retains inline editor and caret');
  body.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'中'}));body.value+='中文';body.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));assert(wb.queue.held,'composition holds save');body.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文'}));assert(!wb.queue.held&&e.payload.content.endsWith('中文'),'composition keeps semantic body');
  body.setSelectionRange(2,4);const base=structuredClone(e),desired={...base,payload:{...base.payload,role:'assistant'}};wb.doc.setPayloadField(e.entry_id,'content','较新正文');wb.doc.applyFields(e.entry_id,base,desired);assert(e.payload.content==='较新正文'&&e.payload.role==='assistant','unrelated payload merges preserve newer body');
  const conflict=wb.doc.applyFields(e.entry_id,base,{...base,payload:{...base.payload,content:'过期正文'}});assert(conflict.conflicts.includes('payload.content')&&e.payload.content==='较新正文','same payload field conflict preserves new intent');
  wb.doc.undo();wb.renderList();
  assert(body.value===e.payload.content&&document.activeElement===body&&body.selectionStart===2,'focused body follows valid structural edit without moving caret');
  wb.list.scrollTop=0;await wb.preview();assert(wb.panel.querySelector('details'),'read only request preview');return assertions;
 })()`);
 await win.webContents.executeJavaScript(String.raw`wb.panel.querySelector('details').open=true`);await shot('03-request-preview');
 await win.webContents.executeJavaScript(String.raw`(async()=>{wb.panel.hidden=true;await wb._toggleRaw();})()`);await shot('04-semantic-structure');
 await win.webContents.executeJavaScript(String.raw`(async()=>{const before=wb.doc.value.entries.length;wb.rawEditor.value='{"schema_version":3,"entries":42}';wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);assert(wb.doc.value.entries.length===before&&wb.doc.value.raw_error,'invalid structure preserves document');wb.rawEditor.value=JSON.stringify({schema_version:3,instructions:wb.doc.value.instructions,entries:wb.doc.value.entries,transformations:[]});wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb._toggleRaw();await wb._sources();})()`);await shot('05-source-picker');
 await win.webContents.executeJavaScript(String.raw`(()=>{wb.panel.hidden=true;const e=wb.doc.value.entries[0];wb.doc.setPayloadField(e.entry_id,'content','长正文示例，保留全文并局部滚动。'.repeat(6000));wb.renderList();wb.list.scrollTop=0;assert(wb.doc.byId.get(e.entry_id).payload.content.length>80000,'long body remains complete');})()`);await shot('06-long-body');
 win.setSize(600,900);await wait(100);await shot('07-narrow');
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  wb._add('unknown_provider_item');const e=wb.doc.value.entries.at(-1);await wait(100);
  const card=wb.viewport.cache.get(e.entry_id),payload=card.payloadEditor;
  payload.value=JSON.stringify({future:{nested:[1,{enabled:true}]},custom_field:'保留未知字段'});
  payload.element.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);
  assert(e.payload.future.nested[1].enabled&&e.payload.custom_field==='保留未知字段','generic payload edits retain unknown shape');
  wb.doc.undo();wb.renderList();assert(JSON.parse(payload.value).future===undefined,'generic payload display follows structural undo');wb.doc.redo();wb.renderList();
  const before=JSON.stringify(e.payload);payload.value='42';payload.element.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);
  assert(JSON.stringify(e.payload)===before&&e.payload_error,'invalid generic payload preserves last valid item');
  const long=FocusPatrolAuthoring.newEntry('message','user');long.payload.content='长正文'.repeat(12000);wb.doc.insert(long);wb.renderList();wb._locate(long.entry_id);await wait(100);
  const text=wb.viewport.cache.get(long.entry_id).fieldEditor('content');text.focus();text.setSelectionRange(2,4);
  wb.doc.setPayloadField(long.entry_id,'content',long.payload.content+'结构修改');wb.renderList();
  assert(text.value===long.payload.content&&text.hasFocus&&text.selectionStart===2,'focused large editor follows structural edit without moving caret');
 })()`);
 fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({assertions:await win.webContents.executeJavaScript('assertions'),screenshots:8,electron:process.versions.electron,chrome:process.versions.chrome},null,2));
 console.log(JSON.stringify(result));win.destroy();app.exit(0);
}
app.whenReady().then(run).catch(e=>{console.error(e);app.exit(1);});
