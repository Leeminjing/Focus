/* 本文件提供完整 Focus 应用容器中的 Patrol 视觉验收。
 * 输入为生产 index.html/app.js、隔离 HTTP 数据和窗口尺寸；输出为十种截图、面板/正文/基础行为不重叠及稳定投放栏断言。
 * 工作流通过生产 bootstrap/openDraft/render 路由装载任务、左右导航和编辑器，受控 API 不连接用户数据库或 Provider。
 * 示例：electron desktop/patrol-shell.e2e.cjs；组件实际 API/图验收由 patrol-workbench-api.e2e.cjs 提供。
 */
const {app,BrowserWindow}=require('electron');
const http=require('node:http'),path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const out=path.resolve(__dirname,'../.tmp/session-patrol-shell');fs.mkdirSync(out,{recursive:true});
app.setPath('userData',path.join(os.tmpdir(),'focus-patrol-shell-'+process.pid));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const task={task_id:'fixture-task',workspace_id:'fixture-workspace',workspace_name:'验收工作区',title:'上下文架构检查',lifecycle:'active',harness_mode:'workspace'};
let draft={draft_id:'fixture-draft',draft_revision:0,task_id:task.task_id,mode:'standard',source_checkpoint_id:'fixture-checkpoint',token_estimate:0,equipment:{model_name:'fixture-model',permissions:['read'],skills:[],access_mode:'read-only'},authoring_document:{schema_version:3,instructions:'检查来源、区分事实与示例，给出可验证的结论。',entries:[],transformations:[]}};
const equipment={models:[{name:'fixture-model',display_name:'验收模型',context_window:128000}],tools:[],skills:[],permissions:['read','write','host_command']};
let server;
async function api(url,method,body){
 if(url.endsWith('/bootstrap'))return {tasks:[task],equipment,plugins:[]};
 if(url.endsWith('/contexts/tree'))return [{context_id:task.task_id,depth:0,parents:[],cache_hit_rate:.75}];
 if(url.endsWith('/drafts/open'))return draft;
 if(url.endsWith('/fixture-draft')&&method==='PUT'){draft={...draft,...body,draft_revision:draft.draft_revision+1};return draft;}
 if(url.endsWith('/source-status'))return {sources:{}};
 if(url.endsWith('/preview'))return {executable:true,preview_token:'visual-only',diagnostics:[],items:[],role_mappings:[],budget:{total:1300,context_window:128000,input:400,tools:200,instructions:100,output_reserve:600},request:{instructions:draft.authoring_document.instructions,input:draft.authoring_document.entries.map(e=>({type:e.kind==='message'?'message':e.kind,...e.payload})),tools:[]}};
 if(url.endsWith('/patrol/sources'))return {contexts:[{title:task.title,context_id:task.task_id,revisions:[{revision_id:'fixture-r1',generation:1}]}],branches:[]};
 if(url.endsWith('/skills'))return {skills:[]};
 if(url.endsWith('/fixture-task'))return {task,messages:[],equipment:draft.equipment,ui_state:{},active_run:null};
 if(url.endsWith('/ui-state'))return {};
 return [];
}
async function run(){
 server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname.startsWith('/desktop/api/')){let raw='';for await(const data of req)raw+=data;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(await api(url.pathname,req.method,raw?JSON.parse(raw):{})));return;}
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
   return {footer:{top:footer.top,bottom:footer.bottom},list:{left:list.left,right:list.right,top:list.top,bottom:list.bottom},panel:{left:panel.left,right:panel.right,top:panel.top,bottom:panel.bottom}};
  };
 })()`);
 const geometry={};
 async function shot(name){await wait(180);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());geometry[name]=await win.webContents.executeJavaScript('bounds()');}
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
 await win.webContents.executeJavaScript('wb.panel.hidden=true;wb._sources()');await shot('04-source-shell');
 await win.webContents.executeJavaScript('wb.panel.hidden=true;wb._toggleRaw()');await shot('05-structure-shell');
 await win.webContents.executeJavaScript(String.raw`(async()=>{await wb._toggleRaw();wb.doc.setPayloadField('m0','content','长正文示例，完整保留并局部滚动。'.repeat(5000));wb.renderList();wb.list.scrollTop=0;})()`);await shot('06-long-shell');
 win.setSize(600,900);await win.webContents.executeJavaScript(String.raw`(()=>{state.inspector.open=false;applyShellLayout(state.shellLayout);renderShellChrome();wb.doc.setPayloadField('m0','content','你负责检查这个问题：保留用户自由编辑的语义。');wb.renderList();wb.list.scrollTop=0;})()`);await shot('07-narrow-shell');
 await win.webContents.executeJavaScript('wb.preview()');await shot('08-narrow-request-shell');
 const assertions=await win.webContents.executeJavaScript('assertions');fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({assertions,geometry,screenshots:10,electron:process.versions.electron,entry:'production index.html/app.js via bootstrap and openDraft',backend:'isolated controlled API, no user database/provider'},null,2));
 console.log(JSON.stringify(assertions));win.destroy();server.close();app.exit(0);
}
app.whenReady().then(run).catch(e=>{console.error(e);server?.close();app.exit(1);});
