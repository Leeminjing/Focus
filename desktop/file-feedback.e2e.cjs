/* 本文件提供隐藏 Electron 的文件反馈与策略编辑验收。
 * 输入为生产页面和离线 HTTP/Live fixture；输出为六条密度、非模态草稿、IME、版本选择和记忆 CRUD 请求断言。
 * 工作流以真实 DOM 操作生产控制器，HTTP 仅替换外部边界；示例：node --test desktop/file-feedback.e2e.cjs。
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
if (!process.versions.electron) {
  require("node:test")("file feedback and strategy editing in Electron", () => {
    const env = { ...process.env, FOCUS_PATROL_TEST_USER_DATA: fs.mkdtempSync(path.join(os.tmpdir(), "focus-file-feedback-")) };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 90000, windowsHide: true });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /FILE_FEEDBACK_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_PATROL_TEST_USER_DATA);
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 960, webPreferences: { contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true, preload: path.join(__dirname, "workspace-patrol-test-preload.cjs") } });
    const run = async code => { try { return await win.webContents.executeJavaScript(code, true); } catch (error) { throw new Error(`Page expression failed: ${code}`, { cause: error }); } };
    const until = async code => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${code})`)) { if (Date.now() > deadline) throw new Error(`Timeout ${code}: ${await run('document.querySelector("[data-patrol-error]")?.textContent')}`); await new Promise(resolve => setTimeout(resolve, 25)); }
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`window.files = Array.from({length:9},(_,i)=>({fact_id:'f'+i,revision:2,kind:'artifact',title:'plan-'+i+'.md',status:'verified',occurred_at:new Date(2026,0,1,0,i).toISOString(),evidence:[{kind:'artifact',metadata:{file:{path:'folder/plan-'+i+'.md',slot_id:'slot',result_key:String(i).repeat(64),sha256:String(i+1).repeat(64),operation:'modified'}}}]}));
      window.memoryRows=[{memory_id:'ordinary',title:'旧记忆',content:'保留内容',source_kind:'manual'},{memory_id:'preset',title:'诊断—重构',category:'evolution_strategy',strategy:{applicability:'不满意',evolution:'先诊断',inheritance:'要求与纠正'},enabled:true}];window.writes=[];
      window.fileFeedbackFetch=async(input,options={})=>{const url=new URL(String(input),'http://focus.test');const p=url.pathname;
        if(p.endsWith('/facts'))return new Response(JSON.stringify({facts:files,has_more:false}));
        if(p.includes('/facts/f')){const fact=files.find(f=>p.endsWith('/'+f.fact_id));if(window.deferFact===fact.fact_id){window.deferFact=null;await new Promise(resolve=>window.resolveFact=resolve);}return new Response(JSON.stringify({fact,revisions:[],file:{...FocusLoopFactsView.fileReference(fact),before:{available:true,text:'old'},after:{available:true,text:'new'}}}));}
        if(p.startsWith('/desktop/api/memory')){if(options.method){const body=options.body?JSON.parse(options.body):{};writes.push({path:p,method:options.method,body});if(options.method==='POST')memoryRows.push({...body,memory_id:'custom'});if(options.method==='PUT')Object.assign(memoryRows.find(m=>p.endsWith('/'+m.memory_id)),body);return new Response(JSON.stringify(body));}return new Response(JSON.stringify({memories:memoryRows}));}
        return null;};
      state.patrolWorkspace={workspace_id:'file-test',display_name:'文件验收',path:'C:/fixture/files'};render();`);
    await until('!document.querySelector("[data-patrol-content]").disabled');
    await run(`document.querySelector('[data-patrol-content]').value='先输出方案';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));document.querySelector('[data-patrol-composer]').requestSubmit()`);
    await until('window.patrolFixture.connected("loop-file-test")');
    await run(`document.querySelector('[data-patrol-composer] [data-patrol-details]').click();for(const fact of files)patrolFixture.emit('loop-file-test','fact',fact.fact_id,2,fact);`);
    await until('document.querySelectorAll(".file-fact").length===9');
    await until('!document.querySelector("[data-patrol-details-content]").inert');
    const density = await run(`(()=>{const s=document.querySelector('[data-patrol-facts]');s.style.flex='none';s.style.height='420px';s.scrollTop=0;const r=s.getBoundingClientRect();return [...s.children].filter(n=>n.getBoundingClientRect().top>=r.top&&n.getBoundingClientRect().bottom<=r.bottom).length})()`);
    assert.ok(density >= 2, `only ${density} timeline entries with sources and evidence`);
    await run(`document.querySelector('[data-patrol-facts]').scrollTop=60;patrolFixture.emit('loop-file-test','fact','f0',2,files[0]);patrolFixture.emit('loop-file-test','fact','f9',2,{...files[8],fact_id:'f9'})`);
    await until('document.querySelectorAll(".file-fact").length===10');
    assert.equal(await run(`document.querySelector('[data-patrol-facts]').scrollTop`),60);
    await run(`const t=document.querySelector('[data-patrol-content]');t.value='中文草稿';t.dispatchEvent(new Event('input'));document.querySelector('button[data-fact-id="f0"]').focus();`);
    assert.equal(await run(`document.activeElement?.dataset.factId`),"f0");
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Input.dispatchKeyEvent", {type:"keyDown",key:"Enter",code:"Enter",windowsVirtualKeyCode:13});
    await win.webContents.debugger.sendCommand("Input.dispatchKeyEvent", {type:"char",text:"\r",unmodifiedText:"\r",key:"Enter",code:"Enter",windowsVirtualKeyCode:13});
    await win.webContents.debugger.sendCommand("Input.dispatchKeyEvent", {type:"keyUp",key:"Enter",code:"Enter",windowsVirtualKeyCode:13});
    await until('!document.querySelector("[data-patrol-file-detail]").hidden');
    assert.deepEqual(await run(`({text:document.querySelector('[data-patrol-content]').value,modal:document.querySelector('[data-patrol-dialog]').open})`), {text:"中文草稿",modal:false});
    await run(`document.querySelector('[data-patrol-file-add]').click()`);
    assert.match(await run(`document.querySelector('[data-patrol-file-refs]').textContent`), /plan-0/);
    assert.equal(await run(`document.activeElement===document.querySelector('[data-patrol-content]')`), true);
    await run(`document.querySelector('[data-patrol-content]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}))`);
    assert.equal(await run(`document.querySelector('[data-patrol-content]').value`), "中文草稿");
    await win.setSize(560, 960);
    assert.equal(await run(`document.querySelector('[data-patrol-content]').value`), "中文草稿");
    await run(`document.querySelector('[data-patrol-file-remove]').click();document.querySelector('[data-patrol-file-picker-open]').click()`);
    await until('document.querySelectorAll("[data-patrol-file-picker] button").length===9');
    await run(`document.querySelectorAll('[data-patrol-file-picker] button')[1].click()`);
    assert.match(await run(`document.querySelector('[data-patrol-file-refs]').textContent`), /plan-1/);
    await run(`window.deferFact='f0';window.resolveFact=null;document.querySelector('button[data-fact-id="f0"]').click()`);
    await until('window.resolveFact');
    await run(`document.querySelector('button[data-fact-id="f1"]').click()`);
    await until('document.querySelector("[data-patrol-file-detail]").textContent.includes("plan-1")');
    await run(`resolveFact()`);
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.match(await run(`document.querySelector('[data-patrol-file-detail]').textContent`), /plan-1/);
    await run(`document.querySelector('[data-patrol-content]').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);
    assert.equal(await run(`document.querySelector('[data-patrol-file-detail]').hidden`),true);
    assert.equal(await run(`document.querySelector('[data-patrol-content]').value`),"中文草稿");
    await run(`document.querySelector('button[data-fact-id="f1"]').click()`);
    await until('!document.querySelector("[data-patrol-file-detail]").hidden');
    win.webContents.invalidate();
    await new Promise(resolve=>setTimeout(resolve,300));
    try { const shot=await win.webContents.capturePage();fs.writeFileSync(path.join(app.getPath('userData'),'file-feedback.png'),shot.toPNG()); } catch (error) { console.log('Screenshot unavailable:',error.message); }
    await win.setSize(1280,960);
    await run(`window.deferFact='f0';window.resolveFact=null;document.querySelector('button[data-fact-id="f0"]').click()`);
    await until('window.resolveFact');
    await run(`state.patrolWorkspace={workspace_id:'other-file-test',display_name:'另一个工作区',path:'C:/fixture/other'};render();resolveFact()`);
    await until('document.querySelector("[data-patrol-content]")');
    assert.equal(await run(`document.querySelector('[data-patrol-file-detail]').hidden`),true);
    await run(`openMemoryView()`);
    await until('document.querySelector("[data-action=new-strategy]")');
    await run(`document.querySelector('[data-action=new-strategy]').click()`);
    await until('document.querySelector("[data-strategy-form]")');
    await run(`for(const [key,value] of Object.entries({title:'自定义策略','strategy:applicability':'不满意','strategy:evolution':'先诊断再重做','strategy:inheritance':'要求和纠正'})){const n=document.querySelector('[data-memory-field="'+key+'"]');n.value=value;n.dispatchEvent(new Event('input',{bubbles:true}));}document.querySelector('[data-action=save-strategy]').click()`);
    await until('writes.some(w=>w.method==="POST"&&w.body.category==="evolution_strategy")');
    await until('!state.memory.composing');
    await run(`state.memory.selectedId='preset';openMemoryCompose(true)`);
    await until('document.querySelector("[data-strategy-form]") && document.querySelector("[data-memory-field=title]").value === "诊断—重构"');
    await run(`document.querySelector('[data-memory-field=enabled]').click();document.querySelector('[data-action=save-strategy]').click()`);
    await until('writes.some(w=>w.method==="PUT"&&w.path.endsWith("/preset"))');
    assert.equal(await run(`writes.find(w=>w.path.endsWith('/preset')).body.enabled`), false);
    await until('!state.memory.composing');
    await run(`state.memory.selectedId='ordinary';openMemoryCompose(true);state.memory.draft.content='更新普通记忆';saveMemory()`);
    assert.equal(await run(`writes.some(w=>w.method==='PUT'&&w.path.endsWith('/ordinary')&&w.body.content==='更新普通记忆')`), true);
    console.log("FILE_FEEDBACK_PASS", JSON.stringify({visibleRows:density,zoom:win.webContents.getZoomFactor(),draft:true,ime:true,memory:true}));
    win.destroy();app.quit();
  }).catch(error=>{console.error(error);app.exit(1);});
}
