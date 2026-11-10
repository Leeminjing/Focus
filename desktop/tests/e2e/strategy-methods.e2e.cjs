/* 使用生产页面与隔离 HTTP fixture 验证方法优先、发布信息折叠、导入保护和窄屏可达性。
 * 运行：node --test desktop/tests/e2e/strategy-methods.e2e.cjs。
 */
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), os = require("node:os");
if (!process.versions.electron) {
  require("node:test")("strategy methods, import comparison and safeguards in Electron", () => {
    const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 45000, windowsHide: true });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /STRATEGY_METHODS_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-strategy-methods-")));
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 960, webPreferences: { contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true, preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs") } });
    const run = async code => { try { return await win.webContents.executeJavaScript(code, true); } catch (error) { throw new Error(`Page expression failed: ${code.slice(0, 180)}`, { cause: error }); } };
    const until = async code => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${code})`)) { if (Date.now() > deadline) throw new Error(`Timeout: ${code}`); await new Promise(resolve => setTimeout(resolve, 25)); }
    };
    const capture = async name => {
      if (!process.env.FOCUS_STRATEGY_QA_DIR) return;
      fs.mkdirSync(process.env.FOCUS_STRATEGY_QA_DIR, { recursive: true });
      await run("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
      fs.writeFileSync(path.join(process.env.FOCUS_STRATEGY_QA_DIR, name + ".png"), (await win.webContents.capturePage()).toPNG());
    };
    const reachable = async selector => {
      assert.equal(await run(`(async () => {
        const button = document.querySelector(${JSON.stringify(selector)}); button.scrollIntoView({block:'center'});
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const r = button.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= innerHeight && document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('button') === button;
      })()`), true, selector);
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until("state.tasks.length > 0");
    await run(`(async () => {window.methodCalls=[];
      window.oldMethod={memory_id:'method',title:'验证方法',category:'evolution_strategy',enabled:true,workspace_id:null,locally_modified:true,installation_hash:'original-hash',
        strategy:{applicability:'存在假设',evolution:'先验证',inheritance:'保留证据'},strategy_package:{id:'test/method',version:'1.0.0',description:'原简介'}};
      window.importPreview={metadata:{id:'test/method',name:'验证方法',version:'2.0.0',description:'更新简介'},strategy:{applicability:'存在关键假设',evolution:'收集证据后再推进',inheritance:'目标与纠正'},existing:oldMethod,
        capacity:[{fits:false,input_bound:50,previous_input_bound:60,strategy_input_limit:40,model_name:'fixture-model',strategies:[]}]};
      window.fileFeedbackFetch=async(input,options={})=>{
        const p=new URL(String(input),'http://focus.test').pathname;
        const json=value=>new Response(JSON.stringify(value));
        if(p==='/desktop/api/memory')return json({memories:[oldMethod]});
        if(p.startsWith('/desktop/api/strategies/')){
          methodCalls.push({path:p,body:JSON.parse(options.body)});
          if(p.endsWith('/preview'))return json(importPreview);
          if(p.endsWith('/install'))return json({status:'replaced',memory:oldMethod});
        }
        if(p==='/desktop/api/memory/method'){methodCalls.push({path:p,body:JSON.parse(options.body)});return json(oldMethod);}
        return null;
      };
      await openMemoryView();state.memory.selectedId='method';openMemoryCompose(true);})()`);
    assert.deepEqual(await run(`Array.from(document.querySelectorAll('[data-strategy-method] textarea'),n=>n.dataset.memoryField)`), ["strategy:applicability", "strategy:evolution", "strategy:inheritance"]);
    assert.equal(await run(`document.querySelector('[data-strategy-publishing]').open`), false);
    assert.equal(await run(`document.querySelector('[data-memory-field="package:id"]').checkVisibility()`), false);
    await capture("strategy-editor");
    await run(`document.querySelector('[data-strategy-publishing] summary').click();
      const field=document.querySelector('[data-memory-field="package:description"]');field.value='保留修改后的简介';field.dispatchEvent(new Event('input',{bubbles:true}));
      document.querySelector('[data-strategy-publishing] summary').click();`);
    await reachable('[data-action="save-strategy"]');
    await run(`document.querySelector('[data-action=save-strategy]').click()`);
    await until("!state.memory.composing");
    assert.equal(await run(`methodCalls.find(c=>c.path==='/desktop/api/memory/method').body.strategy_package.description`), "保留修改后的简介");

    await run(`openStrategyImport();const source=document.querySelector('[data-strategy-import-field=markdown]');source.value='fixture-source';source.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-action=preview-strategy]').click()`);
    await until("document.querySelector('[data-strategy-import-preview]')");
    assert.equal(await run("document.querySelector('[data-strategy-import-source]').open"), false);
    assert.equal(await run("document.querySelector('[data-action=install-strategy]').disabled"), false);
    assert.match(await run("document.querySelector('[data-strategy-import-preview]').textContent"), /包含本地修改/);
    assert.equal(await run(`(() => {
      const columns = document.querySelector('.strategy-import-comparison').children;
      const oldRules = columns[0].querySelectorAll('pre'), newRules = columns[1].querySelectorAll('pre');
      return Array.from(oldRules).every((rule, i) => Math.abs(rule.getBoundingClientRect().top - newRules[i].getBoundingClientRect().top) < 1);
    })()`), true, "corresponding old and new rules must align");
    await run("document.querySelector('.strategy-import').scrollIntoView({block:'start'})");
    await capture("strategy-comparison");
    await win.setContentSize(560, 900);
    await reachable('[data-action="install-strategy"]');
    assert.equal(await run("document.querySelector('#app').scrollWidth <= document.querySelector('#app').clientWidth + 1"), true);
    await capture("strategy-import-narrow");
    await run("document.querySelector('[data-strategy-import-field=enabled]').click()");
    assert.equal(await run("Boolean(document.querySelector('[data-action=install-strategy], [data-action=fork-imported-strategy]'))"), false);
    assert.equal(await run("Boolean(document.querySelector('[data-strategy-import-field=workspace_id]'))"), true);
    await run("document.querySelector('[data-action=preview-strategy]').click()");
    await until("document.querySelector('[data-action=install-strategy]')");
    await run("document.querySelector('[data-action=install-strategy]').click()");
    await until("!state.memory.strategyImport");
    const installed = await run("methodCalls.find(c=>c.path.endsWith('/install')).body");
    assert.equal(installed.expected_hash, "original-hash");
    assert.equal(installed.replace_memory_id, "method");
    assert.equal(installed.enabled, false);

    await run("openStrategyImport();state.memory.strategyImport.markdown='fixture-source';importPreview.capacity[0].input_bound=70;document.querySelector('[data-action=preview-strategy]').click()");
    await until("document.querySelector('[data-action=install-strategy]')");
    assert.equal(await run("document.querySelector('[data-action=install-strategy]').disabled"), true);
    await run("document.querySelector('[data-action=fork-imported-strategy]').click()");
    assert.equal(await run("state.memory.draft.memory_id"), undefined);
    assert.match(await run("state.memory.draft.strategy_package.id"), /^local\//);
    assert.equal(await run("state.memory.draft.strategy.evolution"), "收集证据后再推进");
    await reachable('[data-action="save-strategy"]');
    console.log("STRATEGY_METHODS_PASS"); win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
