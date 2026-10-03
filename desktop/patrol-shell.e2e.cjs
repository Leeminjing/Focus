/* 本文件提供完整 Focus 应用容器中的 Patrol 左右编排视觉与交互验收。
 * 输入为生产 index.html/app.js、隔离 HTTP 数据和窗口尺寸；输出为原位编排/响应式截图、可选录屏帧及面板不重叠/稳定投放栏断言。
 * 工作流通过生产 bootstrap/openDraft/render 路由装载任务、左右导航和编辑器，验证真实材料侧栏增删/规则/版本动作的检查过期、焦点连续性及离开释放；受控 API 不连接用户数据库或 Provider。
 * 示例：FOCUS_PATROL_RECORD=1 electron desktop/patrol-shell.e2e.cjs 记录真实界面；实际 API/图验收由 patrol-workbench-api.e2e.cjs 提供。
 */
const {app,BrowserWindow}=require('electron');
const http=require('node:http'),path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const out=path.resolve(__dirname,'../.tmp/session-patrol-shell');fs.mkdirSync(out,{recursive:true});
app.setPath('userData',path.join(os.tmpdir(),'focus-patrol-shell-'+process.pid));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const recording=process.env.FOCUS_PATROL_RECORD==='1';
const task={task_id:'fixture-task',workspace_id:'fixture-workspace',workspace_name:'验收工作区',title:'上下文架构检查',lifecycle:'active',harness_mode:'workspace'};
let draft={draft_id:'fixture-draft',draft_revision:0,task_id:task.task_id,mode:'standard',source_checkpoint_id:'fixture-checkpoint',token_estimate:0,equipment:{model_name:'fixture-model',permissions:['read'],skills:[],access_mode:'read-only'},authoring_document:{schema_version:3,instructions:'检查来源、区分事实与示例，给出可验证的结论。',entries:[],transformations:[]}};
const equipment={models:[{name:'fixture-model',display_name:'验收模型',context_window:128000}],tools:[],skills:[],permissions:['read','write','host_command']};
let materials=[{material_id:'protected-material',relative_path:'rules.txt',reading_mode:'rough',instruction_mode:'reference',retention:'irreplaceable',size_bytes:100},
 {material_id:'removable-material',relative_path:'notes.txt',reading_mode:'rough',instruction_mode:'reference',retention:'removable',size_bytes:100}];
const sourceEntries=[{entry_id:'source-user',kind:'message',payload:{role:'user',content:'补充来源：先验证当前架构，再编排需要的上下文。'}},
 {entry_id:'source-call',kind:'function_call',payload:{name:'read_file',call_id:'source-read',arguments:'{\"path\":\"architecture.md\"}'}},
 {entry_id:'source-output',kind:'function_call_output',payload:{call_id:'source-read',output:'来源记录：工具调用与结果独立保留。'}}];
let server;
async function api(url,method,body){
 if(url.endsWith('/materials/upload')){materials.push({material_id:'uploaded-material',relative_path:'upload.txt',reading_mode:'rough',instruction_mode:'reference',retention:'removable',size_bytes:100});return {};}
 if(url.endsWith('/fixture-task/materials'))return materials;
 const material=materials.find(item=>url.includes('/materials/'+item.material_id));
 if(material){
  if(url.endsWith('/versions'))return [{version_id:'material-version',source:'fixture',created_at:'2026-10-03'}];
  if(url.endsWith('/clear')){material.size_bytes=0;return material;}
  if(url.endsWith('/restore')){material.size_bytes=100;return material;}
  if(method==='PUT'){Object.assign(material,body);return material;}
  if(method==='DELETE'){materials=materials.filter(item=>item!==material);return {};}
 }
 if(url.endsWith('/bootstrap'))return {tasks:[task],equipment,plugins:[]};
 if(url.endsWith('/contexts/tree'))return [{context_id:task.task_id,depth:0,parents:[],cache_hit_rate:.75}];
 if(url.endsWith('/drafts/open'))return draft;
 if(url.endsWith('/fixture-draft')&&method==='PUT'){draft={...draft,...body,draft_revision:draft.draft_revision+1};return draft;}
 if(url.endsWith('/fixture-draft')&&method==='GET')return draft;
 if(url.endsWith('/source-preview')){
  const rows=sourceEntries.map((entry,index)=>({source_row_id:'source-'+index,kind:entry.kind,role:entry.payload.role,summary:entry.payload.content || entry.payload.output || entry.payload.arguments,eligible:true,truncated:false}));
  if(body.row_id){const index=Number(body.row_id.split('-').at(-1));return {source_ref:body,source_fingerprint:'visual-fingerprint',row:{...rows[index],entry:sourceEntries[index]}};}
  return {source_ref:{kind:'context',context_id:task.task_id,revision_id:'fixture-r1',title:'架构讨论'},source_fingerprint:'visual-fingerprint',rows,total:rows.length,excluded_count:0,next_cursor:null};
 }
 if(url.endsWith('/sources')&&method==='POST'){
  const inserted=sourceEntries.filter((_,index)=>body.selected_row_ids.includes('source-'+index)).map(entry=>({...structuredClone(entry),entry_id:entry.entry_id+'-copy',source_ref:'frozen-'+entry.entry_id,source_hash:'visual-source',source_label:'架构讨论 · R1'}));
  const document=structuredClone(draft.authoring_document),index=body.before_entry_id==null?document.entries.length:document.entries.findIndex(entry=>entry.entry_id===body.before_entry_id);document.entries.splice(index,0,...inserted);
  draft={...draft,authoring_document:document,draft_revision:draft.draft_revision+1};return {...draft,inserted_entries:inserted,inserted_entry_ids:inserted.map(entry=>entry.entry_id)};
 }
 if(url.endsWith('/source-status'))return {sources:{}};
 if(url.endsWith('/preview'))return {executable:true,preview_token:'visual-only',diagnostics:[],items:[],role_mappings:[],budget:{total:1300,context_window:128000,input:400,tools:200,instructions:100,output_reserve:600},request:{instructions:draft.authoring_document.instructions,input:draft.authoring_document.entries.map(e=>({type:e.kind==='message'?'message':e.kind,...e.payload})),tools:[]}};
 if(url.endsWith('/patrol/sources'))return {items:[{label:task.title+' · R1',available:true,source_ref:{kind:'context',context_id:task.task_id,revision_id:'fixture-r1'}}],next_cursor:null};
 if(url.endsWith('/skills'))return {skills:[]};
 if(url.endsWith('/fixture-task'))return {task,messages:[],equipment:draft.equipment,ui_state:{},active_run:null};
 if(url.endsWith('/ui-state'))return {};
 return [];
}
async function run(){
 server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname.startsWith('/desktop/api/')){let raw='';for await(const data of req)raw+=data;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(await api(url.pathname,req.method,raw&&req.headers['content-type']?.includes('application/json')?JSON.parse(raw):{})));return;}
  const file=path.resolve(__dirname,'.'+(url.pathname==='/'?'/index.html':url.pathname));
  if(!file.startsWith(__dirname+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
  const types={'.js':'text/javascript','.css':'text/css','.html':'text/html','.png':'image/png','.svg':'image/svg+xml','.json':'application/json'};
  res.setHeader('Content-Type',types[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const win=new BrowserWindow({show:true,width:1700,height:1080,webPreferences:{contextIsolation:false,nodeIntegration:false,backgroundThrottling:false}});
 win.webContents.on('console-message',event=>{if(event.level==='error')console.error(event.message);});
 await win.loadURL('http://127.0.0.1:'+server.address().port+'/index.html');win.focus();await wait(250);
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  await openDraft('fixture-task');window.wb=FocusPatrolWorkbench.get('fixture-draft');window.assertions={};window.assert=(value,label)=>{if(!value)throw Error(label);assertions[label]=true;};
  assert(document.querySelector('.draft-footer')&&document.querySelector('#appNavigation')&&!document.querySelector('#appInspector').hidden,'production shell navigation inspector and deployment footer');
  window.bounds=()=>{const footer=document.querySelector('.draft-footer').getBoundingClientRect(),list=wb.list.getBoundingClientRect(),panel=wb.panel.getBoundingClientRect(),deploy=document.querySelector('[data-action="deploy"]').getBoundingClientRect();
   assert(!document.querySelector('#globalStatus.danger'),'resize and advanced views produce no page error');
   assert(footer.bottom<=innerHeight&&deploy.right<=innerWidth&&deploy.bottom<=innerHeight,'deployment actions stay inside viewport');
   assert(list.bottom<=footer.top,'document stays above deployment footer');
   if(!wb.panel.hidden){assert(panel.bottom<=footer.top,'advanced panel stays above deployment footer');assert(panel.right<=list.left||panel.left>=list.right||panel.bottom<=list.top||panel.top>=list.bottom,'panel never overlaps document editing area');}
   return {editing_area:{width:wb.root.clientWidth,height:wb.root.clientHeight},footer:{top:footer.top,bottom:footer.bottom},list:{left:list.left,right:list.right,top:list.top,bottom:list.bottom},panel:{left:panel.left,right:panel.right,top:panel.top,bottom:panel.bottom}};
  };
 })()`);
 const geometry={};
 const frames=path.join(out,'frames');if(recording)fs.mkdirSync(frames,{recursive:true});let capturing=false,frame=0;
 const recorder=recording?setInterval(async()=>{if(capturing || win.isDestroyed())return;capturing=true;try{fs.writeFileSync(path.join(frames,String(frame++).padStart(5,'0')+'.jpg'),(await win.webContents.capturePage()).resize({width:1440}).toJPEG(80));}finally{capturing=false;}},150):null;
 async function shot(name){await wait(recording?900:180);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());geometry[name]=await win.webContents.executeJavaScript('bounds()');}
 await shot('01-empty-shell');
 await win.webContents.executeJavaScript(String.raw`(()=>{
  const entries=[['developer','你负责检查这个问题：核对任务事实与运行状态，保留工具交换的语义。'],['user','帮我分析 Focus 的上下文编译与 Provider 投影链路。'],['assistant','我会读取架构实现，检查输入保真与执行边界。']].map(([role,content],i)=>({entry_id:'m'+i,kind:'message',payload:{role,content}}));
  entries.push({entry_id:'call',kind:'function_call',payload:{name:'read_file',call_id:'architecture-read',arguments:'{\n  "path": "backend/app/desktop/session_patrol/compiler.py"\n}'}},{entry_id:'output',kind:'function_call_output',payload:{call_id:'architecture-read',output:'读取结果：AuthoringDocument → FocusItem → 实际模型请求。'}});
  wb.doc.append(entries);wb.renderList();wb.list.scrollTop=0;
 })()`);await shot('02-mixed-shell');
 await win.webContents.executeJavaScript('wb._locate("call")');await shot('09-tool-group-shell');
 await win.webContents.executeJavaScript('wb.list.scrollTop=0;wb.renderList()');
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  const body=wb.viewport.cache.get('m0').fieldEditor('content');body.focus();body.setSelectionRange(2,4);window.activeBody=body;await wb.preview();wb.panel.querySelector('details').open=true;
  assert(document.activeElement===body&&body.selectionStart===2,'request preview retains active editor and caret');
  assert(!document.querySelector('[data-action="deploy"]').disabled,'preview enables production deployment action');
 })()`);await shot('03-request-shell');
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  wb.instructions.closest('details').open=true;wb.instructions.focus();await wb.preview();
  const a=wb.instructions.getBoundingClientRect(),b=wb.panel.getBoundingClientRect();
  assert(document.activeElement===wb.instructions,'request preview retains behavior editor focus');
  assert(b.top>=a.bottom||b.left>=a.right||b.right<=a.left,'advanced panel never covers expanded behavior editor');
 })()`);await shot('10-behavior-request-shell');
 await win.webContents.executeJavaScript("wb.instructions.closest('details').open=false");
 await win.webContents.executeJavaScript(`(async()=>{wb.review.close();await wb.sourceController.choose({kind:'context',context_id:'fixture-task',revision_id:'fixture-r1'});wb.view.setPage('source');})()`);await shot('04-source-shell');
 await win.webContents.executeJavaScript(`(()=>{const checks=[...wb.sourceView.node.querySelectorAll('.patrol-source-row input[type=checkbox]')];checks.forEach(input=>{input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));});wb.interactions.setAnchor('m1');})()`);await shot('11-selected-sources');
 await win.webContents.executeJavaScript(`(async()=>{await wb.interactions.importSelected();assert(wb.doc.value.entries[1].entry_id==='source-user-copy'&&wb.doc.value.entries[3].kind==='function_call_output','selected source batch inserts before stable anchor');assert(wb.sourceController.rows[0].summary.includes('补充来源'),'source text remains read only');wb._locate('source-user-copy');})()`);await shot('12-source-inserted');
 await win.webContents.executeJavaScript(`(()=>{const editor=wb.viewport.cache.get('source-user-copy').fieldEditor('content');editor.value+=' 用户原位改写';editor.dispatchEvent(new InputEvent('input',{bubbles:true}));assert(wb.doc.byId.get('source-user-copy').edited_from==='frozen-source-user','inline editing retains source relation');wb.doc.move('source-user-copy',0);wb.renderList();})()`);await shot('13-inline-edit-reorder');
 await win.webContents.executeJavaScript(`(async()=>{wb._undo(false);wb._undo(false);assert(!wb.doc.byId.has('source-user-copy'),'batch import undo removes whole batch');wb._undo(true);wb._undo(true);await wb.preview();wb.doc.setInstructions('更新基础行为，重新检查');assert(!wb.previewPlan&&wb.root.dataset.previewState==='stale','edit marks actual check stale');assert(document.querySelector('[data-action="deploy"]').disabled,'stale check disables production deploy');})()`);await shot('14-check-stale');
 await win.webContents.executeJavaScript(`(async()=>{await wb.preview();assert(wb.root.dataset.previewState==='valid','fresh check restores valid state');wb.review.close();})()`);await shot('15-fresh-check');
 await win.webContents.executeJavaScript('wb.panel.hidden=true;wb._toggleRaw()');await shot('05-structure-shell');
 await win.webContents.executeJavaScript(String.raw`(async()=>{await wb._toggleRaw();wb.doc.setPayloadField('m0','content','长正文示例，完整保留并局部滚动。'.repeat(5000));wb.renderList();wb.list.scrollTop=0;})()`);await shot('06-long-shell');
 await win.webContents.executeJavaScript(`(()=>{wb.doc.setPayloadField('m0','content','恢复短正文以验证来源拖入');wb.renderList();wb._locate('m0');wb.view.setPage('target');wb.sourceController.select('source-0',true);window.originalIds=wb.doc.value.entries.map(e=>e.entry_id);wb.doc.remove('source-user-copy');wb.renderList();window.dragOrder=wb.doc.value.entries.map(e=>e.entry_id);})()`);await wait(150);
 const points=await win.webContents.executeJavaScript(`(()=>{const handle=wb.sourceView.node.querySelector('[aria-label="拖动选中来源插入"]'),source=handle.getBoundingClientRect(),target=wb.list.getBoundingClientRect();return {start:{x:Math.round(source.x+source.width/2),y:Math.round(source.y+source.height/2)},end:{x:Math.round(target.x+target.width/2),y:Math.round(target.y+42)}};})()`);
 win.webContents.sendInputEvent({type:'mouseDown',...points.start,button:'left',clickCount:1});win.webContents.sendInputEvent({type:'mouseMove',...points.end});await wait(90);
 await win.webContents.executeJavaScript(`assert(wb.interactions.drag?.started&&!wb.interactions.line.hidden&&wb.interactions.line.textContent.includes('1'),'native source drag shows insertion line and count')`);
 win.webContents.sendInputEvent({type:'mouseUp',...points.end,button:'left',clickCount:1});await wait(180);
 await win.webContents.executeJavaScript(`(()=>{assert(wb.doc.byId.has('source-user-copy')&&wb.sourceController.rows.length===3,'native source drop copies without moving source');wb._undo(false);assert(JSON.stringify(wb.doc.value.entries.map(e=>e.entry_id))===JSON.stringify(dragOrder),'native source drop undoes as one batch');const divider=wb.view.divider,ratio=wb.view.ratio;divider.focus();divider.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));assert(wb.view.ratio>ratio&&divider.getAttribute('aria-valuenow'),'keyboard separator adjusts column width');wb.view._setRatio(42);wb._locate('m1');const id=wb.selected,index=wb.doc.value.entries.findIndex(e=>e.entry_id===id);document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',altKey:true,bubbles:true}));assert(wb.doc.value.entries[index+1].entry_id===id,'keyboard ordering retains stable item identity');wb._undo(false);})()`);await shot('16-pointer-and-keyboard');
 for(const [width,height] of [[1440,900],[1280,840],[1024,768]]){win.setSize(width,height);await wait(100);await shot('width-'+width);}
 win.setSize(1700,1080);await win.webContents.executeJavaScript(`wb.root.style.width='680px';wb.root.style.maxWidth='100%'`);await shot('width-680-editing-area');
 await win.webContents.executeJavaScript(`assert(wb.root.clientWidth===680&&wb.root.dataset.patrolNarrow==='true','exact 680px editing area uses preserved-state tabs');wb.root.style.width='';wb.root.style.maxWidth=''`);
 win.setSize(600,900);await win.webContents.executeJavaScript(String.raw`(()=>{state.inspector.open=false;applyShellLayout(state.shellLayout);renderShellChrome();wb.doc.setPayloadField('m0','content','你负责检查这个问题：保留用户自由编辑的语义。');wb.renderList();wb.list.scrollTop=0;})()`);await shot('07-narrow-shell');
 await win.webContents.executeJavaScript('wb.preview()');await shot('08-narrow-request-shell');
 await win.webContents.executeJavaScript(`(()=>{const buffer=wb.doc.byId.get('m0').payload.content;wb.review.close();wb.interactions.setAnchor('m1');wb.list.scrollTop=55;const targetScroll=wb.list.scrollTop;wb.view.setPage('source');wb.sourceController.select('source-0',true);wb.sourceView.list.scrollTop=40;const sourceScroll=wb.sourceView.list.scrollTop;assert(wb.sourceController.rows.length===3,'narrow source state retained');wb.view.setPage('target');assert(wb.doc.byId.get('m0').payload.content===buffer,'narrow tabs retain target buffer');assert(wb.list.scrollTop===targetScroll&&wb.interactions.anchor==='m1'&&wb.sourceController.selected.has('source-0'),'narrow tabs retain scroll selection and insertion anchor');wb.view.setPage('source');assert(wb.sourceView.list.scrollTop===sourceScroll,'narrow source scroll restores');wb.view.setPage('target');})()`);
 win.setSize(1700,1080);await wait(150);
 await win.webContents.executeJavaScript(`(async()=>{window.originalApi=wb.api;const body=wb.viewport.cache.get('m0').fieldEditor('content');body.focus();body.setSelectionRange(2,4);const text=body.value;
  wb.api=async(url,options)=>url.endsWith('/source-preview')?Promise.reject(Object.assign(Error('精确来源版本不存在，请切换版本或刷新'),{status:404})):originalApi(url,options);
  await wb.sourceController.choose({kind:'context',revision_id:'missing'});assert(wb.sourceController.error&&body.value===text&&(body.element?body.hasFocus:document.activeElement===body)&&body.selectionStart===2&&wb.viewport.cache.get('m0').fieldEditor('content')===body,'source failure preserves target input and focus');})()`);await shot('17-source-unavailable');
 await win.webContents.executeJavaScript(`(async()=>{wb.api=originalApi;await wb.sourceController.choose({kind:'context',context_id:'fixture-task',revision_id:'fixture-r1'});assert(wb.sourceController.rows.length===3&&!wb.sourceController.error,'source error recovers by exact version selection');window.originalSave=wb.queue.save;wb.queue.save=async()=>{throw Object.assign(Error('revision conflict'),{status:409,code:'draft_revision_conflict'});};wb.doc.setPayloadField('m0','content','冲突期间保留的中文输入');await wb.flush().catch(()=>{});assert(wb.queue.blocked&&wb.localConflict&&wb.status.textContent.includes('保留'),'save conflict blocks replay and explains recovery');})()`);await shot('18-save-conflict');
 await win.webContents.executeJavaScript(`(async()=>{wb.queue.save=originalSave;await wb._reloadServer();wb._undo(false);assert(wb.doc.byId.get('m0').payload.content==='冲突期间保留的中文输入'&&!wb.queue.blocked,'server reload is undoable without losing local conflict text');await wb.flush();wb.api=async(url,options)=>url.endsWith('/preview')?{executable:false,diagnostics:[{code:'provider_projection',entry_ids:[],message:'全局投影无法准备，请检查完整结构',options:['edit']}]}:originalApi(url,options);await wb.preview();assert(![...wb.panel.querySelectorAll('button')].some(b=>b.textContent.startsWith('定位'))&&[...wb.panel.querySelectorAll('button')].some(b=>b.textContent==='编辑完整 JSON'),'global projection error has recovery and no fake item locator');})()`);await shot('19-global-error');
 await win.webContents.executeJavaScript(`(async()=>{wb.api=originalApi;await wb.preview();assert(wb.previewPlan.executable,'global check error recovers through actual recheck');wb.review.close();})()`);
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  const until=async(predicate)=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,10));}throw Error('material action timeout');};
  state.materials.set(state.activeTaskId,await api('/desktop/api/tasks/fixture-task/materials'));
  state.inspector.open=true;state.inspector.tab='materials';render();
  const root=wb.root,worker=wb.worker,body=wb.viewport.cache.get('m0').fieldEditor('content'),text=body.value;
  const card=id=>document.querySelector('#appInspector [data-material-id="'+id+'"]');
  card('protected-material').querySelector('[data-action="toggle-material"]').click();
  await until(()=>card('protected-material').querySelector('[data-field="reading_mode"]'));
  assert(wb.root===root&&root.isConnected&&wb.worker===worker&&wb.active,'material sidebar expansion preserves patrol mount');
  const stale=async(label)=>{
   await until(()=>wb.review.state==='stale');
   assert(!wb.previewPlan&&document.querySelector('[data-action="deploy"]').disabled&&wb.view.budget.textContent.includes('过期预算'),label+' invalidates budget and deployment');
   assert(wb.root===root&&root.isConnected&&wb.worker===worker&&wb.viewport.cache.get('m0').fieldEditor('content')===body&&body.value===text,label+' preserves editor mount and buffer');
  };
  const rules=card('protected-material');rules.querySelector('[data-field="reading_mode"]').value='full';rules.querySelector('[data-field="instruction_mode"]').value='strict';
  body.focus();body.setSelectionRange(2,4);rules.querySelector('[data-action="save-material"]').click();await stale('material rules');
  assert(document.activeElement===body&&body.selectionStart===2&&body.selectionEnd===4,'material rules response preserves caret and focus');
  await wb.preview();assert(wb.previewPlan?.executable&&!document.querySelector('[data-action="deploy"]').disabled,'material recheck restores deployment');wb.review.close();
  const savedConfirm=window.confirm;window.confirm=()=>true;
  try{
   card('protected-material').querySelector('[data-action="clear-material"]').click();await stale('material clear');await wb.preview();wb.review.close();
   card('protected-material').querySelector('[data-action="load-versions"]').click();await until(()=>card('protected-material').querySelector('[data-action="restore-version"]'));
   card('protected-material').querySelector('[data-action="restore-version"]').click();await stale('material restore');await wb.preview();wb.review.close();
   card('removable-material').querySelector('[data-action="toggle-material"]').click();await until(()=>card('removable-material').querySelector('[data-action="delete-material"]'));
   card('removable-material').querySelector('[data-action="delete-material"]').click();await stale('material delete');
   assert(!card('removable-material'),'material deletion updates actual inspector');
  }finally{window.confirm=savedConfirm;}
  await wb.preview();wb.review.close();await uploadMaterialFile(new File(['fixture'],'upload.txt',{type:'text/plain'}));await stale('material upload');
  assert(card('uploaded-material'),'material upload updates actual inspector');
  state.view='focus';render();assert(!wb.active&&!wb.worker&&!wb.viewport.frame,'real navigation after material actions releases patrol resources');
  state.view='draft';render();assert(wb.root.isConnected&&wb.active&&wb.worker&&wb.review.state==='stale'&&!wb.previewPlan,'reopening keeps material check stale');
  const source=wb.sourceView,controller=wb.sourceController,originalSourceApi=wb.api;
  let versionsDone;wb.api=async(url,options)=>url.endsWith('/materials/protected-material/versions')?new Promise(resolve=>{versionsDone=resolve;}):originalSourceApi(url,options);
  try{
   source.kind.value='material';source.kind.dispatchEvent(new Event('change',{bubbles:true}));await until(()=>controller.catalog.some(item=>item.source_ref.material_id==='protected-material'));
   source.catalog.value=String(controller.catalog.findIndex(item=>item.source_ref.material_id==='protected-material'));source.catalog.dispatchEvent(new Event('change',{bubbles:true}));await until(()=>versionsDone);
   source.kind.value='file';source.kind.dispatchEvent(new Event('change',{bubbles:true}));versionsDone([{version_id:'late-material-version'}]);await new Promise(resolve=>setTimeout(resolve,30));
   assert(controller.kind==='file'&&!controller.ref&&!controller.versions&&!controller.catalogLoading&&controller.rows.length===0,'production type switch rejects late material versions');
   source.kind.value='material';source.kind.dispatchEvent(new Event('change',{bubbles:true}));await until(()=>controller.catalog.length);
   versionsDone=null;source.catalog.value='0';source.catalog.dispatchEvent(new Event('change',{bubbles:true}));await until(()=>versionsDone);
   source.catalog.value='';source.catalog.dispatchEvent(new Event('change',{bubbles:true}));versionsDone([{version_id:'cancelled-material-version'}]);await new Promise(resolve=>setTimeout(resolve,30));
   assert(!controller.ref&&!controller.versions&&!controller.selected.size&&!controller.catalogLoading,'production cancelled material selection stays empty');
  }finally{wb.api=originalSourceApi;}
 })()`);
 await shot('20-material-changes-stale');
 clearInterval(recorder);while(capturing)await wait(20);
 const assertions=await win.webContents.executeJavaScript('assertions');fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({assertions,geometry,screenshots:Object.keys(geometry).length,recorded_frames:frame,electron:process.versions.electron,entry:'production index.html/app.js via bootstrap and openDraft',backend:'isolated controlled API, no user database/provider'},null,2));
 console.log(JSON.stringify(assertions));win.destroy();server.close();app.exit(0);
}
app.whenReady().then(run).catch(e=>{console.error(e);server?.close();app.exit(1);});
