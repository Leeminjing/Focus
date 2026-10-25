/* 生产记忆整理页：窄矮窗口、分段模式与极端拖拽比例下的阅读高度和保存入口可达性。
 * 示例：node desktop/tests/e2e/memory-compose-layout.e2e.cjs。
 */
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 30000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-memory-layout-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 850, height: 480, webPreferences: { preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true } });
    const run = code => win.webContents.executeJavaScript(code);
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await run(`state.view='memory';state.memory={...state.memory,composing:true,collectedSources:Array.from({length:12},(_,i)=>({kind:'text',title:'来源 '+i,rawText:'原始材料内容。'.repeat(100)})),draft:{title:'整理后的记忆',content:'压缩总结。'.repeat(100),contentMode:'complete'},editorRatio:.5,sourceRatio:.5};render()`);
    for (const [width,height] of [[850,480],[700,480],[390,600],[1200,720]]) {
      win.setContentSize(width,height);
      for (const mode of ["complete","segmented"]) {
        for (const ratio of [.2,.8]) {
          const result = await run(`(async()=>{
            state.memory.editorRatio=${ratio};state.memory.sourceRatio=${ratio};state.memory.draft.contentMode='${mode}';
            state.memory.draft.segments=Array.from({length:6},(_,i)=>({title:'段落 '+i,body:'段落内容。'.repeat(80)}));renderMemory();
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            const sources=document.querySelector('.memory-editor-sources'),target=document.querySelector('.memory-editor-target'),list=sources.querySelector('.memory-source-edit-list');
            const sourceRect=sources.getBoundingClientRect(),targetRect=target.getBoundingClientRect();
            const body=target.querySelector('textarea');
            const result={width:innerWidth,height:innerHeight,mode:'${mode}',ratio:${ratio},stacked:targetRect.top>=sourceRect.bottom,
              sourceHeight:sourceRect.height,targetHeight:targetRect.height,listHeight:list.clientHeight,textHeight:body.getBoundingClientRect().height};
            list.scrollTop=200;result.sourceScrolls=list.scrollTop>0;
            const sourceText=list.querySelector('textarea');sourceText.value='继续编辑来源';sourceText.dispatchEvent(new Event('input',{bubbles:true}));
            body.value='继续编辑目标';body.dispatchEvent(new Event('input',{bubbles:true}));
            const save=document.querySelector('[data-action=save-memory]');save.scrollIntoView({block:'center'});
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            const r=save.getBoundingClientRect();result.saveReachable=r.top>=0&&r.bottom<=innerHeight&&document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('[data-action=save-memory]')===save;
            result.draftPreserved=state.memory.collectedSources[0].rawText==='继续编辑来源'&&(state.memory.draft.contentMode==='complete'?state.memory.draft.content==='继续编辑目标':state.memory.draft.segments[0].body==='继续编辑目标');
            return result;
          })()`);
          const label=JSON.stringify(result);
          if(width<=900) {
            assert.ok(result.stacked&&result.sourceHeight>=280&&result.targetHeight>=280,label);
            assert.ok(result.listHeight>=160&&result.textHeight>=160,label);
          } else assert.equal(result.stacked,false,label);
          assert.ok(result.sourceScrolls&&result.saveReachable&&result.draftPreserved,label);
          console.log(label);
          if(process.env.FOCUS_MEMORY_LAYOUT_EVIDENCE&&width===850&&mode==='complete'&&ratio===.2) {
            fs.mkdirSync(process.env.FOCUS_MEMORY_LAYOUT_EVIDENCE,{recursive:true});
            for(const pane of ['sources','target']) {
              await run(`document.querySelector('.memory-editor-${pane}').scrollIntoView({block:'center'})`);
              await new Promise(resolve=>setTimeout(resolve,120));
              fs.writeFileSync(path.join(process.env.FOCUS_MEMORY_LAYOUT_EVIDENCE,`memory-${pane}.png`),(await win.webContents.capturePage()).toPNG());
            }
          }
        }
      }
    }
    console.log("Memory compose layout: PASS (16 layouts, complete/segmented, editing and save visibility)");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
