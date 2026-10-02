/* 本文件对外提供文档结构撤销及保存竞态确定性回归。
 * 输入为编辑操作、旧字段基线和可控 Promise，输出为 100 次撤销、字段冲突/合并、生命周期保护及保存恢复断言。
 * 工作流使用生产模块；示例：node --test desktop/patrol-document.test.cjs。
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { PatrolDocument, DraftSaveQueue } = require('./patrol-document.js');
test('delayed fields merge preserves newer text and unknown blocks and is reversible', () => {
  const entry={entry_id:'e',role:'assistant',content:'original',future:[{type:'unknown'}],name:'old',source_ref:'s'};
  const doc=new PatrolDocument({entries:[entry]}),base=structuredClone(entry),desired={...base,name:'new'};
  doc.setField('e','content','newer text');
  assert.deepEqual(doc.applyFields('e',base,desired),{conflicts:[],changed:['name']});
  assert.equal(entry.content,'newer text');assert.equal(entry.name,'new');assert.deepEqual(entry.future,base.future);
  doc.undo();assert.equal(entry.name,'old');assert.equal(entry.content,'newer text');doc.redo();assert.equal(entry.name,'new');
});
test('same-field conflict preserves all edits and structural lifetimes invalidate pending entry work', () => {
  const entry={entry_id:'e',role:'user',content:'old',extra:'keep'},doc=new PatrolDocument({entries:[entry]});
  const base=structuredClone(entry),epoch=doc.versionOf('e');doc.setField('e','content','newer');
  const result=doc.applyFields('e',base,{...base,content:'other',extra:'change'});
  assert.deepEqual(result.conflicts,['content']);assert.equal(entry.extra,'keep');assert.equal(entry.content,'newer');
  doc.insert({entry_id:'other'});doc.move('e',1);assert.equal(doc.versionOf('e'),epoch);
  doc.remove('e');doc.undo();assert.notEqual(doc.versionOf('e'),epoch);
});
test('unparsed newer content is protected while unrelated fields can still merge', () => {
  const entry={entry_id:'e',role:'user',content:[{type:'text',text:'old'}],name:'old'},doc=new PatrolDocument({entries:[entry]});
  const base=structuredClone(entry);
  assert.deepEqual(doc.applyFields('e',base,{...base,content:[{type:'text',text:'older fields intent'}]},{pendingFields:['content']}).conflicts,['content']);
  assert.deepEqual(entry.content,base.content);
  assert.equal(doc.applyFields('e',base,{...base,name:'new'},{pendingFields:['content']}).conflicts.length,0);
  assert.equal(entry.name,'new');
});
test('unknown JSON property names remain own data without changing entry prototype', () => {
  const entry={entry_id:'e',role:'user',content:'body'},doc=new PatrolDocument({entries:[entry]}),base=structuredClone(entry);
  const desired=JSON.parse('{"entry_id":"e","role":"user","content":"body","__proto__":{"literal":true}}');
  const prototype=Object.getPrototypeOf(entry);doc.applyFields('e',base,desired);
  assert.equal(Object.getPrototypeOf(entry),prototype);assert.equal(Object.hasOwn(entry,'__proto__'),true);
  assert.equal(JSON.parse(JSON.stringify(entry)).__proto__.literal,true);doc.undo();assert.equal(Object.hasOwn(entry,'__proto__'),false);
});
test('100 structural operations are reversible and unknown blocks survive', () => {
  const doc = new PatrolDocument({schema_version:2, entries:[], unknown:{future:true}});
  for (let i=0;i<100;i++) doc.insert({entry_id:String(i),role:'tool',content:[{type:'future',value:i}]});
  for (let i=0;i<100;i++) assert.equal(doc.undo(), true);
  assert.equal(doc.value.entries.length, 0);
  for (let i=0;i<100;i++) doc.redo();
  assert.equal(doc.value.entries.length,100); assert.deepEqual(doc.value.unknown,{future:true});
});
test('field edits keep the same document and entry, copies have independent identities', () => {
  const entry={entry_id:'e1',role:'assistant',content:'a',source_ref:'s1'}, value={entries:[entry]};
  const doc=new PatrolDocument(value); doc.setField('e1','content','b');
  assert.equal(doc.value,value); assert.equal(doc.byId.get('e1'),entry); assert.equal(entry.edited_from,'s1');
  const copy=doc.copy('e1'); assert.notEqual(copy.entry_id,entry.entry_id); doc.setField(copy.entry_id,'content','c'); assert.equal(entry.content,'b');
});
test('one in flight and a newer edit queues one latest revision', async () => {
  let resolve, text='first'; const writes=[];
  const queue=new DraftSaveQueue({delay:10000,revision:7,snapshot:revision=>({revision,text}),save:body=>{writes.push(body);return new Promise(done=>{resolve=done;});}});
  queue.changed(); const first=queue.flush(); await Promise.resolve(); text='latest'; queue.changed();
  assert.equal(writes.length,1); resolve({draft_revision:8}); await new Promise(done=>setImmediate(done));
  assert.deepEqual(writes[1],{revision:8,text:'latest'}); resolve({draft_revision:9}); await first;
  assert.equal(queue.confirmed,2); assert.equal(queue.serverRevision,9);
});
test('failure retains pending edits and can retry without replacing content', async () => {
  let fail=true; const queue=new DraftSaveQueue({delay:10000,snapshot:r=>({draft_revision:r}),save:async()=>{if(fail)throw Error('offline');return {draft_revision:1};}});
  queue.changed(); await assert.rejects(queue.flush(),/offline/); assert.equal(queue.confirmed,0);
  fail=false; await queue.flush(); assert.equal(queue.confirmed,1);
});
test('composition edits stay dirty after an older save confirms and flush after commit', async () => {
  let resolve, text='before';const writes=[];
  const queue=new DraftSaveQueue({delay:10000,snapshot:revision=>({revision,text}),save:body=>{writes.push(body);return new Promise(done=>{resolve=done;});}});
  queue.changed();const first=queue.flush();await Promise.resolve();
  queue.held=true;text='中文合成中';queue.changed(true);resolve({draft_revision:1});await first;
  assert.equal(queue.confirmed,1);assert.equal(queue.localRevision,2);assert.equal(writes.length,1);
  assert.equal(await queue.flush(),false);
  queue.held=false;queue.changed();const committed=queue.flush();await Promise.resolve();
  assert.deepEqual(writes[1],{revision:1,text:'中文合成中'});resolve({draft_revision:2});await committed;
  assert.equal(queue.confirmed,3);
});
test('source mutations serialize with saves and batch import has one undo', async () => {
  const writes=[];let release;
  const queue=new DraftSaveQueue({delay:10000,snapshot:r=>({revision:r}),save:async body=>{writes.push(body);return {draft_revision:body.revision+1};}});
  const first=queue.mutate(async()=>{await new Promise(resolve=>{release=resolve;});queue.serverRevision++;});
  while(!release)await new Promise(resolve=>setImmediate(resolve));
  queue.changed();const second=queue.mutate(async()=>{queue.serverRevision++;});
  release();await Promise.all([first,second]);
  assert.deepEqual(writes,[{revision:1}]);assert.equal(queue.serverRevision,3);
  const doc=new PatrolDocument({entries:[]});doc.append([{entry_id:'a',content:[{type:'image',url:'data'}]},{entry_id:'b',unknown:true}]);
  assert.equal(doc.value.entries.length,2);doc.undo();assert.equal(doc.value.entries.length,0);doc.redo();assert.equal(doc.value.entries[1].unknown,true);
});
