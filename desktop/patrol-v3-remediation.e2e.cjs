/* 本文件提供 Patrol v3 修复的真实原生键盘与异步面板验收。
 * 输入为隔离生产组件草稿和可控延迟 API；输出为 Ctrl+Z/Redo、类型切换与面板所有权断言。
 * 工作流为逐个激活长正文/块/通用 payload/条目结构/全文结构，发送 Electron 原生键盘事件；延迟导入仍保存所属文档。
 * 示例：electron desktop/patrol-v3-remediation.e2e.cjs；不连接用户数据。
 */
const {app,BrowserWindow}=require('electron');
const path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const out=path.resolve(__dirname,'../.tmp/session-patrol-v3-remediation');fs.mkdirSync(out,{recursive:true});
app.setPath('userData',path.join(os.tmpdir(),'focus-patrol-v3-remediation-'+process.pid));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function run(){
 const win=new BrowserWindow({show:true,width:1280,height:900,webPreferences:{contextIsolation:false,nodeIntegration:false,backgroundThrottling:false}});
 win.webContents.on('console-message',(_event,level,message)=>{if(level>=2)console.error(message);});
 await win.loadFile(path.join(__dirname,'patrol-workbench-fixture.html'));win.focus();
 await win.webContents.executeJavaScript(String.raw`(()=>{
  localStorage.clear();window.assertions={};window.wait=ms=>new Promise(r=>setTimeout(r,ms));
  window.assert=(value,label)=>{if(!value)throw Error(label);assertions[label]=true;};
  const entries=[{entry_id:'long',kind:'message',payload:{role:'user',content:'large text '.repeat(4000)}},
    {entry_id:'blocks',kind:'message',payload:{role:'user',content:[{type:'input_text',text:'text'},{type:'input_image',image_url:'https://example.com/fake.png'},{type:'future',nested:{kept:true}}]}},
    {entry_id:'generic',kind:'unknown',payload:{future:'kept'}}];
  window.draft={draft_id:'v3-remediation',draft_revision:0,task_id:'fake-task',equipment:{},authoring_document:{schema_version:3,instructions:'',entries,transformations:[]}};
  window.baseApi=async(url,options)=>url.endsWith('/source-status')?{sources:{}}:url.endsWith('/preview')?{executable:true,diagnostics:[],request:{input:[],tools:[]},items:[]}:url.includes('/materials')?[]:url.endsWith('/sources')&&!options?{contexts:[],branches:[]}:{draft_revision:JSON.parse(options?.body||'{}').draft_revision+1};
  window.wb=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft,api:baseApi});
 })()`);
 for(const mode of ['long','blocks','generic','entry','raw']){
  console.log('keyboard',mode);
  await win.webContents.executeJavaScript(`(async()=>{
   if(!wb.raw.hidden)await wb._toggleRaw();wb.panel.hidden=true;
   if(${JSON.stringify(mode)}==='raw'){await wb._toggleRaw();window.editor=wb.rawEditor;}
   else {const id=${JSON.stringify(mode)}==='entry'?'long':${JSON.stringify(mode)};wb._locate(id);const card=wb.viewport.cache.get(id);
    if(${JSON.stringify(mode)}==='entry'){card.node.querySelector('.patrol-item-structure').open=true;await wait(30);window.editor=card.structureEditor;}
    else window.editor=${JSON.stringify(mode)}==='generic'?card.payloadEditor:card.fieldEditor('content');}
   editor.focus();editor.setSelectionRange(0,0);window.before=editor.value;window.ops=wb.doc.undoStack.length;window.ids=wb.doc.value.entries.map(e=>e.entry_id).join();
  })()`);
  win.webContents.sendInputEvent({type:'char',keyCode:'X'});await wait(30);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control']});await wait(60);
  await win.webContents.executeJavaScript(`assert(editor.value===before&&wb.doc.undoStack.length===ops&&wb.doc.value.entries.map(e=>e.entry_id).join()===ids,${JSON.stringify(mode+' native undo is exclusive')})`);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control','shift']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control','shift']});await wait(60);
  await win.webContents.executeJavaScript(`assert(editor.value==='X'+before&&wb.doc.undoStack.length===ops,${JSON.stringify(mode+' native redo is exclusive')})`);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control']});await wait(650);
 }
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  if(!wb.raw.hidden)await wb._toggleRaw();
  const inserted=FocusPatrolAuthoring.newEntry('message','user');window.structuralId=inserted.entry_id;wb.doc.insert(inserted);wb.renderList();wb._locate(inserted.entry_id);wb.cards.get(inserted.entry_id).focus();
 })()`);
 win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control']});await wait(60);
 await win.webContents.executeJavaScript(`assert(!wb.doc.byId.has(structuralId),'document undo outside text editor')`);
 win.webContents.sendInputEvent({type:'keyDown',keyCode:'Z',modifiers:['control','shift']});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Z',modifiers:['control','shift']});await wait(60);
 await win.webContents.executeJavaScript(`assert(wb.doc.byId.has(structuralId),'document redo outside text editor')`);
 await win.webContents.executeJavaScript(String.raw`(async()=>{
  await wb.flush();
  for(const outcome of ['success','failure','remount','leave']){
   let resolve,reject;wb.api=async(url,options)=>url.endsWith('/sources')&&options?.method==='POST'?new Promise((a,b)=>{resolve=a;reject=b;}):baseApi(url,options);
   const imported=wb._import({kind:'context',revision_id:'fake'});await wait(30);await wb._sources();const panel=wb.panel;
   if(outcome==='remount')wb.mount(wb.root);
   if(outcome==='leave'){wb.leave();wb.root.remove();}
   if(outcome==='failure')reject(Error('late failure'));else resolve({draft_revision:wb.queue.serverRevision+1,authoring_document:{entries:[{entry_id:'import-'+outcome,kind:'message',payload:{role:'user',content:'frozen'}}]}});
   await imported;await wait(40);
   if(outcome==='success'||outcome==='failure')assert(panel.querySelector('h3')?.textContent==='导入冻结来源','late '+outcome+' preserves newer source panel');
   if(outcome==='remount')assert(wb.panel.textContent==='','late import preserves remounted panel');
   if(outcome!=='failure')assert(wb.doc.byId.has('import-'+outcome),'late '+outcome+' merges into owning document');
   if(outcome==='leave'){const root=document.createElement('div');root.id='fixture';document.body.append(root);wb.mount(root);}
  }
  wb.api=baseApi;await wb.flush();
 })()`);
 const assertions=await win.webContents.executeJavaScript('assertions');fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({assertions,electron:process.versions.electron},null,2));console.log(JSON.stringify(assertions));win.destroy();app.exit(0);
}
app.whenReady().then(run).catch(e=>{console.error(e);app.exit(1);});
