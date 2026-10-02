/* 本文件对外提供 authoring shape 与生产 Worker 协议回归。
 * 输入为合法自由文档及错误 role/元字段/转换；输出为原结构不被改变、路径诊断和请求身份保留断言。
 * 工作流加载生产 validator 与 Worker，允许任意正文、未知字段及角色，拒绝文档结构错误。
 * 示例：node --test desktop/patrol-authoring.test.cjs。
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const {validateDocument} = require('./patrol-authoring.js');
test('free roles unknown JSON and incomplete exchanges remain authorable', () => {
  const value={schema_version:2,instructions:'',entries:[{role:'future-role',content:{type:['future']},extension:{a:[1,2]}},{role:'tool',tool_call_id:'orphan',content:'example'}],future:{kept:true}};
  const before=structuredClone(value);assert.equal(validateDocument(value),null);assert.deepEqual(value,before);
});
test('known structural errors are located without mutating raw values', () => {
  for (const [field,patch] of [['role',{entries:[{role:3}]}],['entry_id',{entries:[{entry_id:''}]}],['source_ref',{entries:[{source_ref:3}]}],['reference_only',{entries:[{reference_only:3}]}],['instructions',{instructions:[]}],['parameters',{transformations:[{document_hash:'h',entry_ids:[],operation:'as_text',parameters:[]}]}]]) {
    const value={schema_version:2,entries:[],...patch},before=structuredClone(value);
    assert.ok(validateDocument(value).includes(field));assert.deepEqual(value,before);
  }
});
test('worker validates documents and fields but freely parses content with exact request identity', () => {
  const replies=[],context=vm.createContext({postMessage:value=>replies.push(value)});context.self=context;
  context.importScripts=file=>vm.runInContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'patrol-parser-worker.js'),'utf8'),context);
  context.onmessage({data:{generation:7,raw:JSON.stringify({schema_version:2,entries:[{role:3}]})}});
  assert.equal(replies[0].generation,7);assert.ok(replies[0].error.includes('role'));assert.equal(replies[0].value,undefined);
  context.onmessage({data:{kind:'fields',request_id:8,entry_id:'e',raw:'{"kind":"message","payload":{},"source_ref":[]}'}});
  assert.equal(replies[1].request_id,8);assert.ok(replies[1].error.includes('source_ref'));
  context.onmessage({data:{kind:'content',request_id:9,entry_id:'e',raw:'[{"type":{"future":true}}]'}});
  assert.equal(replies[2].request_id,9);assert.equal(replies[2].value[0].type.future,true);
  context.onmessage({data:{kind:'format',view_id:4,generation:10,value:{schema_version:2,entries:[]}}});
  assert.equal(replies[3].view_id,4);assert.equal(replies[3].generation,10);assert.equal(JSON.parse(replies[3].raw).entries.length,0);
  context.onmessage({data:{kind:'format',view_id:5,generation:11,raw:'{unfinished 中文',value:{entries:[]}}});
  assert.equal(replies[4].raw,'{unfinished 中文');
});
