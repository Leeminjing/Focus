/* 本文件对外提供左右编排的来源选择、稳定插入和保存竞态合同验收。
 * 输入为真实 document/controller/interactions 与可控延迟 API；输出为跨页身份、原位 buffer、单次撤销和不确定结果恢复断言。
 * 具体工作流为浏览/切版/取消时拒绝迟到目录和版本 → 延迟导入期间继续字段编辑并排队结构命令 → 合并后保存；不替换生产状态对象。
 * 示例：node --test desktop/patrol-assembly.test.cjs。
 */
const test=require('node:test'),assert=require('node:assert/strict');
const {PatrolDocument,DraftSaveQueue}=require('./patrol-document.js');
const {PatrolSourceController}=require('./patrol-source-controller.js');
global.window=globalThis;require('./patrol-interactions.js');
const {PatrolInteractions}=global.FocusPatrolInteractions;
const row=(id)=>({source_row_id:id,kind:'message',eligible:true,summary:id});
const page=(ref,rows,next=null)=>({source_ref:ref,source_fingerprint:ref.revision_id,rows,total:100,next_cursor:next});

test('source selection survives filtering/pages but late responses cannot cross revisions',async()=>{
  let release;const ref={kind:'context',revision_id:'R1'};
  const controller=new PatrolSourceController({draft:{draft_id:'d'},api:async(_url,options)=>{
    const body=JSON.parse(options.body);
    if(body.revision_id==='R2')return new Promise(resolve=>{release=resolve;});
    return page(body,body.cursor?[row('second')]:[row('first')],body.cursor?null:'page-two');
  }});
  await controller.choose(ref);controller.select('first',true);await controller.nextPage();controller.select('second',true);
  assert.deepEqual([...controller.selected],['first','second']);await controller.search('filter');assert(controller.selected.has('second'));
  const late=controller.choose({kind:'context',revision_id:'R2'});await Promise.resolve();
  await controller.choose({kind:'context',revision_id:'R3'});release(page({kind:'context',revision_id:'R2'},[row('wrong')]));await late;
  assert.equal(controller.source.revision_id,'R3');assert.equal(controller.rows[0].source_row_id,'first');assert.equal(controller.selected.size,0);
});

test('batch insertion uses stable anchor, preserves later field edits and undoes once',()=>{
  const document={schema_version:3,entries:[{entry_id:'a',kind:'message',payload:{content:'原文',future:[1]}},{entry_id:'b',kind:'message',payload:{content:'保留'}}]};
  const doc=new PatrolDocument(document);const inserted={entry_id:'new',kind:'function_call',payload:{call_id:'c',name:'read',arguments:'{}'},source_ref:'frozen'};
  doc.insertBatch([inserted,inserted],'b');doc.setPayloadField('a','content','中文改写');assert.deepEqual(document.entries.map(entry=>entry.entry_id),['a','new','b']);
  doc.undo();assert.deepEqual(document.entries.map(entry=>entry.entry_id),['a','b']);assert.equal(document.entries[0].payload.content,'中文改写');assert.deepEqual(document.entries[0].payload.future,[1]);
  doc.redo();assert.equal(document.entries[1].source_ref,'frozen');assert.throws(()=>doc.insertBatch([inserted],'missing'),/不存在/);
});

function workbench(api){
  const wb={draft:{draft_id:'d',draft_revision:4},api,cards:new Map(),status:{textContent:''},sourceView:{render(){}},renderList(){},_refreshSources(){}};
  wb.sourceController=new PatrolSourceController({draft:wb.draft,api});wb.sourceController.ref={kind:'context',revision_id:'R1'};wb.sourceController.source=wb.sourceController.ref;wb.sourceController.fingerprint='R1';wb.sourceController.rows=[row('source')];wb.sourceController.select('source',true);
  wb.doc=new PatrolDocument({schema_version:3,entries:[{entry_id:'a',kind:'message',payload:{content:'原文'}},{entry_id:'b',kind:'message',payload:{content:'目标'}}]},()=>wb.queue.changed());
  wb.queue=new DraftSaveQueue({revision:4,delay:10000,save:body=>api('save',{body:JSON.stringify(body)}),snapshot:revision=>({draft_revision:revision,authoring_document:wb.doc.value})});wb.interactions=new PatrolInteractions(wb);return wb;
}

test('delayed import leaves typing live, queues structure, then saves both against new revision',async()=>{
  let release,importedRequest;const writes=[];
  const wb=workbench(async(url,options)=>{
    const body=JSON.parse(options?.body||'{}');
    if(url==='save'){writes.push(body);return {draft_revision:body.draft_revision+1};}
    importedRequest=body;return new Promise(resolve=>{release=resolve;});
  });
  wb.interactions.setAnchor('b');const importing=wb.interactions.importSelected();
  while(!release)await new Promise(resolve=>setImmediate(resolve));
  wb.doc.setPayloadField('b','content','导入期间中文输入');wb.interactions.run(()=>wb.doc.move('a',2));
  assert.deepEqual(wb.doc.value.entries.map(entry=>entry.entry_id),['a','b']);assert.equal(importedRequest.before_entry_id,'b');
  release({draft_revision:5,inserted_entries:[{entry_id:'new',kind:'message',payload:{content:'来源原文'}}],inserted_entry_ids:['new']});await importing;await wb.queue.flush();
  assert.deepEqual(wb.doc.value.entries.map(entry=>entry.entry_id),['new','b','a']);assert.equal(wb.doc.byId.get('b').payload.content,'导入期间中文输入');assert.equal(writes[0].draft_revision,5);
  assert.equal(wb.sourceController.selected.size,0);wb.doc.undo();wb.doc.undo();assert(!wb.doc.byId.has('new'));assert.equal(wb.doc.byId.get('b').payload.content,'导入期间中文输入');await wb.queue.flush();
});

test('ambiguous import is read before recovery and never automatically replayed',async()=>{
  let imports=0,reads=0;
  const wb=workbench(async(url)=>{if(url.endsWith('/sources')){imports++;throw Error('response lost');}reads++;return {draft_revision:5};});
  await wb.interactions.importSelected();assert.equal(imports,1);assert.equal(reads,1);assert(wb.queue.blocked);assert(wb.localConflict);assert.equal(wb.doc.byId.get('a').payload.content,'原文');assert(!wb.interactions.busy);
});

test('cancel and replacement of stale transformations are structurally reversible',()=>{
  const doc=new PatrolDocument({entries:[],transformations:[{document_hash:'old',entry_ids:['a'],operation:'as_text',parameters:{}}]});
  doc.transform({document_hash:'new',entry_ids:['a'],operation:'placeholder',parameters:{}},0);assert.equal(doc.value.transformations.length,1);doc.cancelTransformation(0);assert.equal(doc.value.transformations.length,0);
  doc.undo();assert.equal(doc.value.transformations[0].document_hash,'new');doc.undo();assert.equal(doc.value.transformations[0].document_hash,'old');
});

test('historical policy changes invalidate pending full row requests',async()=>{
  let release;const controller=new PatrolSourceController({draft:{draft_id:'d'},api:async(_url,options)=>{
    const body=JSON.parse(options.body);if(body.row_id)return new Promise(resolve=>{release=resolve;});
    return {...page(body,[row('same')]),source_fingerprint:body.include_historical?'historical':'default'};
  }});
  await controller.choose({kind:'context',revision_id:'R1'});const detail=controller.expand('same');await controller.setHistorical(true);
  release({source_fingerprint:'default',row:{entry:{kind:'message',payload:{content:'old policy'}}}});await detail;
  assert.equal(controller.fingerprint,'historical');assert.equal(controller.details.size,0);assert.equal(controller.expanded.size,0);
});

test('large material directories and exact versions remain bounded without choosing unseen rows',async()=>{
  const controller=new PatrolSourceController({draft:{draft_id:'d',task_id:'t'},api:async(url,options)=>url.endsWith('/materials')?
    Array.from({length:1000},(_,i)=>({material_id:'m'+i,relative_path:'file-'+i})):url.endsWith('/versions')?
    Array.from({length:1000},(_,i)=>({version_id:'version-'+i})):page(JSON.parse(options.body),[row('only')])});
  await controller.directory('material');for(let i=0;i<19;i++)await controller.directory('material',true);
  assert.equal(controller.catalog.length,100);assert.equal(controller.catalog.at(-1).source_ref.material_id,'m999');assert.equal(controller.ref,null);
  const ref=controller.catalog[0].source_ref;await controller.materialVersions(ref);assert.equal(controller.versions.length,51);
  for(let i=0;i<19;i++)await controller.materialVersions(ref,true);
  assert.equal(controller.versions.length,50);assert.equal(controller.versions.at(-1).source_ref.version_id,'version-999');assert.equal(controller.ref,ref);assert.equal(controller.selected.size,0);
});

test('refresh recovers a changed source from a stale page and requires selecting the new snapshot',async()=>{
  let changed=false;const controller=new PatrolSourceController({draft:{draft_id:'d'},api:async(_url,options)=>{
    const body=JSON.parse(options.body);if(changed && body.cursor)throw Object.assign(Error('invalid cursor'),{status:422});
    return {...page(body,[row('same')],'old-page'),source_fingerprint:changed?'new':'old'};
  }});
  await controller.choose({kind:'file',path:'current.txt'});controller.select('same',true);changed=true;
  await controller.nextPage();assert.equal(controller.error,'invalid cursor');await controller.refresh();
  assert.equal(controller.page,0);assert.equal(controller.fingerprint,'new');assert.equal(controller.selected.size,0);assert.match(controller.error,/来源已变化/);
});

for(const intent of ['file','cancel','exact-version','other-material'])test(`late material versions cannot override ${intent} intent`,async()=>{
  let release;const previews=[];
  const controller=new PatrolSourceController({draft:{draft_id:'d',task_id:'t'},api:async(url,options)=>{
    if(url.endsWith('/materials/old/versions'))return new Promise(resolve=>{release=resolve;});
    if(url.endsWith('/versions'))return [{version_id:'new-version'}];
    const body=JSON.parse(options.body);previews.push(body);return page(body,[row('selected')]);
  }});
  const old={kind:'material',context_id:'t',material_id:'old'};
  const pending=controller.materialVersions(old);
  let expected=null;
  if(intent==='file')await controller.setKind('file');
  if(intent==='cancel')await controller.choose(null);
  if(intent==='exact-version'){expected={...old,version_id:'exact'};await controller.choose(expected);controller.select('selected',true);}
  if(intent==='other-material'){expected={...old,material_id:'new'};await controller.materialVersions(expected);controller.select('selected',true);}
  release([{version_id:'late'}]);await pending;
  assert.equal(controller.ref,expected);assert.equal(controller.catalogLoading,false);
  assert.equal(previews.some(body=>body.material_id==='old'&&!body.version_id),false);
  if(expected){assert(controller.selected.has('selected'));assert.equal(controller.rows[0].source_row_id,'selected');}
  else {assert.equal(controller.versions,null);assert.equal(controller.selected.size,0);assert.equal(controller.rows.length,0);}
});

test('late directory failures cannot leak into a new source kind',async()=>{
  let reject;const controller=new PatrolSourceController({draft:{draft_id:'d'},api:()=>new Promise((_resolve,no)=>{reject=no;})});
  const pending=controller.directory();await controller.setKind('file');reject(Error('old catalog failed'));await pending;
  assert.equal(controller.kind,'file');assert.equal(controller.error,null);assert.equal(controller.catalogLoading,false);assert.deepEqual(controller.catalog,[]);
});
