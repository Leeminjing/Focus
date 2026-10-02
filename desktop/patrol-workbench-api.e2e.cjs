/* 本文件对外提供前台 Electron 与真实隔离 Desktop API 的交互验收。
 * 输入为测试 bridge URL 和冻结 fixture JSON 路径；输出为字段交错/同字段冲突、raw shape、来源失效及部署断言。
 * 工作流加载生产模块，经实际 API 保存文档/编译/运行；slow/failure 只作用测试网络，模型采样由 Python fixture 控制。
 * 示例：测试设置 FOCUS_PATROL_TEST_URL/FIXTURE 后启动 electron desktop/patrol-workbench-api.e2e.cjs。
 */
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
app.setPath('userData',path.join(os.tmpdir(),`focus-patrol-api-${process.pid}`));
async function run(){
  const url=process.env.FOCUS_PATROL_TEST_URL,fixturePath=process.env.FOCUS_PATROL_TEST_FIXTURE,fixture=JSON.parse(fs.readFileSync(fixturePath,'utf8'));
  const win=new BrowserWindow({show:true,width:1280,height:900,webPreferences:{contextIsolation:false,nodeIntegration:false,backgroundThrottling:false}});
  await win.loadFile(path.join(__dirname,'patrol-workbench-fixture.html'));win.focus();
  const result=await win.webContents.executeJavaScript(`(async()=>{
    const fixture=${JSON.stringify(fixture)},base=${JSON.stringify(url)};
    const assert=(value,message)=>{if(!value)throw Error(message);};
    const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const api=async(route,options={})=>{const response=await fetch(base+route,{...options,headers:{'Content-Type':'application/json'}});const data=await response.json();if(!response.ok)throw Error(JSON.stringify(data));return data;};
    const wb=FocusPatrolWorkbench.mount(document.querySelector('#fixture'),{draft:fixture.draft,api});await wb.flush();
    const behavior=wb.root.querySelector('[aria-label="小兵基础行为"]');behavior.value='界面编写的小兵基础行为';behavior.dispatchEvent(new InputEvent('input',{bubbles:true}));await wb.flush();
    wb.doc.insert({entry_id:'field-race',role:'user',content:'ORIGINAL',name:'original-name',future:{preserved:true}});wb.select('field-race');
    const fieldDetail=wb.editor.querySelector('details');fieldDetail.open=true;await wait(30);
    const fieldEditor=fieldDetail.querySelector('textarea'),textEditor=wb.editor.querySelector('[aria-label="条目正文"]');
    const fieldValue=JSON.parse(fieldEditor.value);fieldValue.name='changed-name';fieldEditor.value=JSON.stringify(fieldValue);fieldEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));
    textEditor.focus();textEditor.value='NEWER_TEXT';textEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));textEditor.setSelectionRange(2,4);
    await wait(700);await wb.flush();
    const persistedRace=(await api('/desktop/api/drafts/'+fixture.draft.draft_id)).authoring_document.entries.find(e=>e.entry_id==='field-race');
    assert(persistedRace.content==='NEWER_TEXT'&&persistedRace.name==='changed-name'&&persistedRace.future.preserved,'field parse preserves newer body and unknown fields after actual save');
    assert(wb.editor.querySelector('[aria-label="条目正文"]')===textEditor&&document.activeElement===textEditor&&textEditor.selectionStart===2&&textEditor.selectionEnd===4,'field result preserves editor focus and caret');
    fieldValue.name='changed-name';fieldValue.content='CONFLICTING_TEXT';fieldEditor.value=JSON.stringify(fieldValue);fieldEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(700);await wb.flush();
    assert(wb.doc.byId.get('field-race').content==='NEWER_TEXT'&&wb.doc.byId.get('field-race').fields_error.includes('content')&&wb.doc.byId.get('field-race').fields_buffer.includes('CONFLICTING_TEXT'),'same-field conflict retains both intents');
    fieldValue.content='NEWER_TEXT';fieldEditor.value=JSON.stringify(fieldValue);fieldEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));
    wb.doc.insert({entry_id:'navigate-race',role:'user',content:'OTHER_EDITOR'});wb.select('navigate-race');const otherEditor=wb.editor.querySelector('[aria-label="条目正文"]');otherEditor.focus();await wait(700);await wb.flush();
    assert(wb.doc.byId.get('field-race').content==='NEWER_TEXT'&&!wb.doc.byId.get('field-race').fields_error&&otherEditor.value==='OTHER_EDITOR'&&document.activeElement===otherEditor,'late parse stays with original entry after navigation');
    wb.select('field-race');await wb._toggleRaw();
    const validDocument=structuredClone(wb.doc.value);delete validDocument.raw_buffer;delete validDocument.raw_error;
    const invalidDocuments=[{...validDocument,entries:[{entry_id:'e',role:3}]},{...validDocument,instructions:[]},{...validDocument,entries:[{entry_id:'e',role:'user',source_ref:3}]},{...validDocument,transformations:[{document_hash:'h',entry_ids:[],operation:'as_text',parameters:[]}]}];
    for(const invalid of invalidDocuments){
      wb.rawEditor.value=JSON.stringify(invalid);wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb.flush();
      const preserved=(await api('/desktop/api/drafts/'+fixture.draft.draft_id)).authoring_document;
      assert(preserved.entries.find(e=>e.entry_id==='field-race').content==='NEWER_TEXT'&&preserved.raw_error&&preserved.raw_buffer===JSON.stringify(invalid),'schema-invalid raw persists beside last valid body');
    }
    wb.rawEditor.value=JSON.stringify(validDocument);wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb._toggleRaw();wb.doc.remove('field-race');wb.doc.remove('navigate-race');wb.select(null);await wb.flush();
    const cancelledRaw=wb._toggleRaw();assert(wb.rawEditor.readOnly&&!wb.rawState.hidden,'raw formatting gives immediate pending feedback');await wb._toggleRaw();
    assert(await cancelledRaw===false&&wb.raw.hidden,'raw view cancellation ignores pending format');
    const focusPending=wb._toggleRaw();wb.instructions.closest('details').open=true;wb.instructions.focus();await focusPending;
    assert(document.activeElement===wb.instructions,'late formatting preserves newer focus intent');await wb._toggleRaw();
    const blocks=[{type:'text',text:'原块正文'},{type:'image_url',image_url:{url:'data:image/png;base64,iVBORw0KGgo='}},{type:'future',payload:{kept:true}}];
    wb.doc.insert({entry_id:'mixed-ui',role:'user',content:blocks});wb.select('mixed-ui');
    const blockEditor=wb.editor.querySelector('[aria-label="条目正文"]'),changedBlocks=structuredClone(blocks);changedBlocks[0].text='改写块正文';blockEditor.value=JSON.stringify(changedBlocks);blockEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb.flush();
    const blocksSaved=await api('/desktop/api/drafts/'+fixture.draft.draft_id),mixed=blocksSaved.authoring_document.entries.find(e=>e.entry_id==='mixed-ui');
    assert(mixed.content[0].text==='改写块正文'&&JSON.stringify(mixed.content.slice(1))===JSON.stringify(blocks.slice(1)),'visual mixed blocks preserve image and unknown payload');
    const blockDetails=wb.editor.querySelector('details');blockDetails.open=true;await wait(30);const blockFields=blockDetails.querySelector('textarea');
    const conflictingBlocks=JSON.parse(blockFields.value);conflictingBlocks.content[0].text='字段里的旧块意图';blockFields.value=JSON.stringify(conflictingBlocks);blockFields.dispatchEvent(new InputEvent('input',{bubbles:true}));
    changedBlocks[0].text='较新块正文';blockEditor.value=JSON.stringify(changedBlocks);blockEditor.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(700);await wb.flush();
    assert(wb.doc.byId.get('mixed-ui').content[0].text==='较新块正文'&&wb.doc.byId.get('mixed-ui').fields_error.includes('content')&&blockEditor.value.includes('较新块正文'),'newer unparsed blocks win without discarding conflicting fields buffer');
    await wb._toggleRaw();assert(JSON.parse(wb.rawEditor.value).entries[0].content[2].payload.kept,'mixed structured view');await wb._toggleRaw();wb.doc.remove('mixed-ui');wb.select(null);await wb.flush();
    await wb._sources();assert(wb.panel.querySelector('[aria-label="精确来源版本"]'),'source picker');
    await wb._import({kind:'file',context_id:fixture.draft.task_id,path:'ui-source.txt'});
    const source=wb.doc.value.entries[0];assert(source.content==='界面来源','frozen source');wb.select(source.entry_id);
    const editor=wb.editor.querySelector('[aria-label="条目正文"]');editor.focus();editor.value+=' 用户改写';editor.dispatchEvent(new InputEvent('input',{bubbles:true}));await wb.flush();
    assert(wb.doc.byId.get(source.entry_id).edited_from===source.source_ref,'edited source identity');
    await api('/test/delete-source',{method:'POST'});await wb._refreshSources();
    assert(wb.sourceLabel.textContent.includes('原来源不可用')&&wb.doc.byId.get(source.entry_id).content.includes('用户改写')&&wb.editor.querySelector('[aria-label="条目正文"]')===editor,'deleted source is marked without replacing frozen text or editor');
    await wb._toggleRaw();wb.rawEditor.value='{ unfinished 中文';wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb.flush();
    const reopened=await api('/desktop/api/tasks/'+fixture.draft.task_id+'/drafts/open',{method:'POST'});
    assert(reopened.authoring_document.raw_buffer==='{ unfinished 中文'&&reopened.authoring_document.raw_error,'raw persisted through actual API');
    const previous=wb.doc.value.raw_buffer;await wb._reloadServer();assert(wb.doc.value.raw_buffer===previous&&wb.queue.serverRevision===reopened.draft_revision,'explicit actual API reload');
    const value=structuredClone(wb.doc.value);delete value.raw_buffer;delete value.raw_error;
    value.entries.push({entry_id:'ui-orphan',role:'tool',tool_call_id:'missing',content:'模拟结果',future:{preserved:true}});
    wb.rawEditor.value=JSON.stringify(value);wb.raw.dispatchEvent(new InputEvent('input',{bubbles:true}));await wait(650);await wb._toggleRaw();
    let plan=await wb.preview();assert(!plan.executable&&plan.diagnostics.some(d=>d.entry_ids.includes('ui-orphan')),'located tool conflict');
    wb._locate('ui-orphan');assert(document.activeElement===wb.editor.querySelector('[aria-label="条目正文"]'),'diagnostic focus');
    const diagnostic=plan.diagnostics.find(d=>d.entry_ids.includes('ui-orphan'));
    wb.doc.transform({document_hash:plan.document_hash,entry_ids:diagnostic.entry_ids,operation:'as_text',parameters:{}});plan=await wb.preview();assert(plan.executable,'explicit repair');
    await api('/test/fail-next-save',{method:'POST'});wb.doc.setField(source.entry_id,'content','失败后保留的编辑');
    let failed=false;try{await wb.flush();}catch(_){failed=true;}assert(failed&&wb.queue.confirmed<wb.queue.localRevision,'failed save is recoverable');
    await wb.flush();assert(wb.doc.byId.get(source.entry_id).content==='失败后保留的编辑','retry preserved text');
    wb.doc.clear();wb._undo(false);assert(wb.doc.value.entries.length===2,'batch undo');plan=await wb.preview();
    assert(plan.diagnostics.some(d=>d.code==='stale_transformation'),'edited document invalidates repair');
    wb.doc.value.transformations=[];wb._changed();plan=await wb.preview();
    wb.doc.transform({document_hash:plan.document_hash,entry_ids:['ui-orphan'],operation:'as_text',parameters:{}});plan=await wb.preview();assert(plan.executable,'renewed explicit repair');
    assert(plan.request.instructions.includes('界面编写的小兵基础行为'),'behavior reaches actual instructions');
    const run=await api('/desktop/api/drafts/'+fixture.draft.draft_id+'/deploy',{method:'POST',body:JSON.stringify({deployment_id:crypto.randomUUID(),preview_token:plan.preview_token})});
    let terminal;for(let i=0;i<200;i++){terminal=await api('/desktop/api/runs/'+run.run_id);if(terminal.status==='success')break;await wait(25);}assert(terminal.status==='success','actual graph completion');
    const branchRoot=document.createElement('div');document.body.append(branchRoot);let cloned;
    await FocusPatrolBranches.mount(branchRoot,{agent:{agent_id:run.agent_id,latest_run:terminal},api,onRun:()=>{},onDraft:draft=>{cloned=draft;},onError:error=>{throw error;}});
    const selection=FocusPatrolBranches.get(run.agent_id);assert(selection.run_id===run.run_id&&selection.checkpoint_id===terminal.final_checkpoint_id,'exact branch selection');
    branchRoot.querySelector('button').click();for(let i=0;i<100&&!cloned;i++)await wait(25);assert(cloned&&cloned.authoring_document.entries.length===2,'edit frozen definition');
    return {field_merge:true,field_conflict:true,field_navigation:true,invalid_shape_buffer:true,source_unavailable:true,sources:true,raw_save:true,mixed_blocks:true,unknown_fields:true,diagnostics:true,explicit_transform:true,failed_save_retry:true,undo:true,deployment:true,branches:true,definition_edit:true,run_id:run.run_id};
  })()`);
  console.log(JSON.stringify(result));win.destroy();app.exit(0);
}
app.whenReady().then(run).catch(error=>{fs.writeFileSync(path.join(os.tmpdir(),'focus-patrol-api-error.log'),String(error.stack||error));console.error(error);app.exit(1);});
