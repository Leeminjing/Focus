/* 使用生产页面与隔离 API 验证 Context 信息层级、浏览器操作菜单及输入框真实点击命中。
 * 运行：node desktop/tests/e2e/patrol-context-layout.e2e.cjs。
 * FOCUS_WORKBENCH_EVIDENCE 可指定截图和几何报告目录。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");

if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], {
    env, encoding: "utf8", windowsHide: true, timeout: 90000,
  });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || "");
  if (result.error) console.error(result.error);
  process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("force-device-scale-factor", "1");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-context-layout-")));
  const output = process.env.FOCUS_WORKBENCH_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), "focus-context-evidence-"));
  fs.mkdirSync(output, { recursive: true });
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1748, height: 904, useContentSize: true, webPreferences: {
      preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"),
      contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    } });
    const run = code => win.webContents.executeJavaScript(code, true);
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async code => {
      const deadline = Date.now() + 10000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > deadline) {
          fs.writeFileSync(path.join(output, "timeout.png"), (await win.webContents.capturePage()).toPNG());
          throw new Error(`Timeout: ${code}; ${await run(`document.querySelector('[data-patrol-context-content]')?.innerHTML`)}`);
        }
        await sleep(20);
      }
    };
    const pointer = async point => {
      const coordinates = { x: Math.round(point.x * win.webContents.getZoomFactor()), y: Math.round(point.y * win.webContents.getZoomFactor()) };
      win.webContents.sendInputEvent({ type: "mouseDown", ...coordinates, button: "left", clickCount: 1 });
      win.webContents.sendInputEvent({ type: "mouseUp", ...coordinates, button: "left", clickCount: 1 });
      await sleep(50);
    };
    const click = async selector => pointer(await run(`(() => {
      const node = document.querySelector(${JSON.stringify(selector)});
      node.scrollIntoView({block: 'nearest'});
      const r = node.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()`));
    const failures = [], results = [], menus = [];
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until('document.querySelector("[data-patrol-content]")');
    await run(`window.browserMenuRequests = []; window.contextFixtureFetch = window.fetch;
      window.fetch = async (input, options = {}) => {
        const path = new URL(String(input), 'http://focus.test').pathname;
        if (!path.startsWith('/desktop/api/contexts/root/browser')) return contextFixtureFetch(input, options);
        browserMenuRequests.push({path, method:options.method || 'GET'});
        return new Response(JSON.stringify(path.endsWith('/pages') ? {pages:[]} : path.endsWith('/downloads') ? {downloads:[],permissions:[]} : {context_id:'root',state:'available',shared_context_ids:[]}), {status:200,headers:{'Content-Type':'application/json'}});
      };
      patrolFixture.seedHistory('context-layout', []);
      patrolFixture.graphConversations = true;
      patrolFixture.lineage = {complete:true, roots:{root:'r2'}, nodes:[{context_id:'root',revision_id:'r2',generation:2,title:'Context 展开验收'}],edges:[]};
      state.patrolWorkspace = {workspace_id:'context-layout',display_name:'Context 布局验收',path:'C:/isolated/context-layout'}; render();`);
    await until('patrolFixture.connected("loop-context-layout") && !document.querySelector("[data-patrol-content]").disabled');
    await run(`patrolFixture.emit('loop-context-layout', 'context', 'root', 1, {title:'Context 布局验收',status:'active',current_revision_id:'r2'})`);
    await click('[data-patrol-tools-toggle]');
    await click('[data-patrol-composer] [data-patrol-details]');
    await until('document.querySelector("[data-patrol-lineage] [data-context-id=root]")');
    await run(`Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))`);
    await run(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
    assert.equal(await run(`(() => {
      const detail=document.querySelector('[data-patrol-context]'), column=document.querySelector('.workbench-timeline-column'), timeline=column.querySelector('.patrol-facts-card');
      return detail.hidden && detail.inert && !detail.querySelector('[data-patrol-context-content]').childElementCount
        && document.querySelector('[data-workbench-show=context]').hidden
        && Math.abs(timeline.getBoundingClientRect().height-column.getBoundingClientRect().height)<2;
    })()`), true, '未选择时不生成空态，时间线使用释放出的空间');
    fs.writeFileSync(path.join(output, 'unselected.png'), (await win.webContents.capturePage()).toPNG());
    await run(`document.querySelector('[data-patrol-lineage] [data-context-id=root]').click()`);
    await until('document.querySelector("[data-patrol-context-tab=conversation]")');
    await run(`Promise.all(document.getAnimations().filter(animation => animation.effect.getComputedTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})))`);
    assert.equal(await run(`document.querySelector('[data-patrol-context-content]').firstElementChild.tagName`), "HEADER", "工作线标题应在工具之前");
    assert.equal(await run(`Boolean(document.querySelector('[data-patrol-context-content] > header [data-context-inspector-menu] .context-browser-controls'))`), true, "浏览器工具应收纳到标题操作菜单");
    for (const action of ['present', 'pages', 'downloads', 'binding']) {
      await click('[data-context-inspector-actions]');
      await click(`[data-context-inspector-menu] [data-action=context-browser-${action}]`);
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]').matches(':popover-open')`), false, `${action} 应收起操作菜单`);
      if (action !== 'present') {
        await until('document.querySelector(".context-browser-dialog[open]")');
        if (action === 'pages') await until('document.querySelector(".context-browser-page-list").textContent.includes("尚无页面")');
        if (action === 'downloads') await until('document.querySelector(".context-browser-interaction-list").textContent.includes("尚无下载记录")');
        if (action === 'binding') assert.ok(await run(`Boolean(document.querySelector('.context-browser-dialog[open] select'))`));
        await run(`document.querySelector('.context-browser-dialog[open]').close()`);
      }
    }
    assert.equal(await run(`browserMenuRequests.filter(item => item.path.endsWith('/present') && item.method === 'POST').length`), 2);

    for (const [width, height, zoom] of [[1748,904,1], [1440,900,1], [1200,720,1], [1100,800,1], [1000,720,1], [900,700,1], [600,700,1], [1440,700,1.25]]) {
      win.setContentSize(width, height); win.webContents.setZoomFactor(zoom); await sleep(100);
      await run(`if (document.querySelector('#app').clientWidth < 1200) document.querySelector('[data-workbench-show=context]').click()`);
      await run(`document.querySelector('[data-patrol-context-tab=overview]')?.click()`);
      await run(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
      assert.equal(await run(`document.querySelector('[data-patrol-context-content] > header h2').textContent`), 'Context 布局验收');
      assert.equal(await run(`document.querySelector('[data-patrol-context-status]').textContent`), 'active');
      const summary = await run(`(() => { const panel=document.querySelector('[data-patrol-context]'); const body=panel.querySelector('.patrol-context-body').getBoundingClientRect();const sources=panel.querySelector('.workbench-context-sources').getBoundingClientRect();return {body:body.toJSON(),sources:sources.toJSON()}; })()`);
      assert.ok(summary.sources.bottom <= summary.body.bottom + 1, `来源摘要应优先可见 ${width}: ${JSON.stringify(summary)}`);
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]').matches(':popover-open')`), false);
      fs.writeFileSync(path.join(output, `${width}-${height}-${zoom}-overview.png`), (await win.webContents.capturePage()).toPNG());
      await click('[data-context-inspector-actions]');
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]').matches(':popover-open')`), true);
      const menu = await run(`(() => {
        const menu = document.querySelector('[data-context-inspector-menu]');
        const actions = [...menu.querySelectorAll('button')];
        return {rect:menu.getBoundingClientRect().toJSON(), viewport:[innerWidth,innerHeight], hit:actions.every(button => {
          const r=button.getBoundingClientRect(); return button.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
        })};
      })()`);
      assert.ok(menu.hit && menu.rect.top >= 0 && menu.rect.bottom <= menu.viewport[1]+1 && menu.rect.left >= 0 && menu.rect.right <= menu.viewport[0]+1, JSON.stringify(menu));
      menus.push({width, height, zoom, ...menu});
      fs.writeFileSync(path.join(output, `${width}-${height}-${zoom}-menu.png`), (await win.webContents.capturePage()).toPNG());
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'}); await sleep(30);
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]')?.matches(':popover-open')`), false);
      assert.equal(await run(`Boolean(document.querySelector('[data-patrol-context-tab=conversation]'))`), true, 'Escape 应先关闭操作菜单');
      await run(`document.querySelector('[data-context-inspector-actions]').focus()`);
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Enter'});
      win.webContents.sendInputEvent({type:'char',keyCode:'Enter'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Enter'}); await sleep(30);
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]').matches(':popover-open')`), true, '键盘应能打开操作菜单');
      await click('[data-patrol-content]');
      assert.equal(await run(`document.querySelector('[data-context-inspector-menu]').matches(':popover-open')`), false, '点击外部应收起菜单');
      for (const tab of ["conversation", "sources"]) {
        await run(`document.querySelector('[data-patrol-context-tab=${tab}]').click()`);
        await until(`document.querySelector('.patrol-selected-card.is-expanded [data-patrol-context-tab=${tab}][aria-pressed=true]')`);
        for (const delivery of [false, true]) {
          await run(`(() => { const delivery = document.querySelector('[data-patrol-delivery]'); delivery.hidden = ${!delivery}; delivery.textContent = '本次交付已完成，可以继续输入。';
            document.querySelector('[data-patrol-content]').value = '展开后继续发送';
            document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input'));
            document.querySelector('[data-patrol-composer]').scrollIntoView({block:'nearest'}); })()`);
          await sleep(40);
          const label = `${width}x${height}@${zoom} ${tab} delivery=${delivery}`;
          const geometry = await run(`(() => {
            const panel = document.querySelector('.patrol-selected-card');
            const composer = document.querySelector('[data-patrol-composer]');
            const input = document.querySelector('[data-patrol-content]');
            const send = composer.querySelector('button[type=submit]');
            const rect = element => element.getBoundingClientRect().toJSON();
            const hit = element => { const r = rect(element); return element.contains(document.elementFromPoint(r.x+r.width/2, r.y+r.height/2)); };
            const a = rect(panel), b = rect(composer);
            return {hostWidth:document.querySelector('#app').clientWidth, panel:a, composer:b, input:rect(input), send:rect(send),
              overlaps:a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top,
              inputHit:hit(input), sendHit:hit(send), submissions:patrolFixture.submissions.length};
          })()`);
          await pointer({x:geometry.input.x+geometry.input.width/2, y:geometry.input.y+geometry.input.height/2});
          const inputFocused = await run(`document.activeElement === document.querySelector('[data-patrol-content]')`);
          await click('[data-patrol-composer] button[type=submit]');
          await until(`patrolFixture.submissions.length >= ${geometry.submissions+1}`);
          const submitted = await run(`patrolFixture.submissions.length === ${geometry.submissions+1}`);
          results.push({label, ...geometry, inputFocused, submitted});
          if (geometry.overlaps || !geometry.inputHit || !geometry.sendHit || !inputFocused || !submitted) {
            failures.push(label);
            console.error(`FAIL ${label}: ${JSON.stringify(geometry)} inputFocused=${inputFocused} submitted=${submitted}`);
          }
          if (!delivery) fs.writeFileSync(path.join(output, `${width}-${height}-${zoom}-${tab}.png`), (await win.webContents.capturePage()).toPNG());
        }
      }
    }
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({results, menus, browserActions:['present','pages','downloads','binding'], failures}, null, 2));
    assert.equal(failures.length, 0, `Context 覆盖输入或阻止点击：${failures.join(', ')}`);
    assert.equal(await run('patrolFixture.mainSubmissions.length'), 0, "只读检查不应改变发送目标");
    assert.equal(await run(`patrolFixture.submissions.every(item => item.content === '展开后继续发送')`), true);
    // 用现有工作区 store 检查精确回复身份；查看、切换页签和关闭不写入草稿。
    await run(`workspacePatrolIntake.store('context-layout').edit({content:'保留的指定回复',request_id:'question-fixed',request_revision:7});
      window.contextDraft=JSON.stringify(workspacePatrolIntake.store('context-layout').get().draft)`);
    for (const width of [1748, 1100, 390]) {
      win.webContents.setZoomFactor(1); win.setContentSize(width, 904); await sleep(100);
      await run(`document.querySelector('[data-workbench-show=graph]').click();
        document.querySelector('[data-patrol-lineage] [data-context-id=root]').click()`);
      await until(`document.querySelector('[data-patrol-context]').dataset.revisionId==='r2'`);
      assert.equal(await run(`(() => {
        const panel=document.querySelector('[data-patrol-context]');
        return !panel.hidden && !panel.inert && panel.checkVisibility() && panel.dataset.contextId==='root'
          && document.getElementById(panel.getAttribute('aria-labelledby')).textContent==='Context 布局验收'
          && document.getElementById(panel.getAttribute('aria-describedby')).textContent==='R2 · r2';
      })()`), true, '详情区域对应对象、固定版本和可访问标题');
      await run(`document.querySelector('[data-patrol-context-tab=sources]').click()`);
      await until(`document.querySelector('[data-patrol-inspect-revision=r2]')`);
      await run(`document.querySelector('[data-patrol-context-close]').click()`);
      assert.equal(await run(`(() => {
        const panel=document.querySelector('[data-patrol-context]');
        return panel.hidden && panel.inert && !panel.querySelector('[data-patrol-context-content]').childElementCount
          && document.querySelector('[data-workbench-show=context]').hidden
          && document.querySelector('.workspace-patrol').dataset.workbenchPanel==='graph'
          && document.activeElement===document.querySelector('[data-patrol-lineage] [data-context-id=root]')
          && JSON.stringify(workspacePatrolIntake.store('context-layout').get().draft)===contextDraft;
      })()`), true, '关闭收起详情、恢复关系图和原入口焦点，并保留指定回复身份');
    }
    await run(`(async () => {
      const check=(value,label)=>{if(!value)throw new Error(label)};
      const panel=document.createElement('aside'); panel.className='patrol-selected-card'; panel.innerHTML='<div data-patrol-context-content></div>'; document.body.append(panel);
      const input=document.querySelector('[data-patrol-content]'), replies=[], reads=[];
      const inspector=FocusContextInspector.create({presentation:'workbench',getManifest:()=>({nodes:[],edges:[{source_context_id:'a',source_revision_id:'a-r1',target_context_id:'b',target_revision_id:'b-r2'}],revisions:[]}),
        api:{conversation:(_loop,context,options)=>new Promise(resolve=>{reads.push({context,revision:options.revisionId,signal:options.signal});replies.push(resolve)})}});
      inspector.mount(panel);
      const first=inspector.select('loop','a',{title:'对象 A'},input,'a-r1');
      const second=inspector.select('loop','b',{title:'对象 B'},input,'b-r2');
      check(reads[0].signal.aborted,'切换对象未取消旧读请求');
      replies[1]({revision:{revision_id:'b-r2',generation:2},messages:[]});await second;
      replies[0]({revision:{revision_id:'a-r1',generation:1},messages:[]});await first;
      check(panel.dataset.contextId==='b'&&panel.dataset.revisionId==='b-r2','迟到响应改变对象或版本');
      inspector.refresh();check(reads.length===2&&inspector.selection().revisionId==='b-r2','刷新丢失固定版本');
      const pending=inspector.select('loop','c',{title:'对象 C'},input,'c-r3');
      inspector.close();check(reads[2].signal.aborted&&panel.hidden&&panel.inert,'关闭未取消读取或隐藏详情');
      replies[2]({revision:{revision_id:'c-r3'},messages:[]});await pending;
      check(panel.hidden&&!panel.textContent&&inspector.selection()===null,'关闭后的迟到响应重新展开详情');
      const reopened=inspector.select('loop','b',{title:'对象 B'},input,'b-r2');
      check(reads[3].revision==='b-r2','重开未请求指定版本');
      replies[3]({revision:{revision_id:'b-r2',generation:2},messages:[]});await reopened;
      panel.querySelector('[data-context-related=a]').click();
      check(reads[4].context==='a'&&reads[4].revision==='a-r1','来源跳转未保持指定对象及版本');
      replies[4]({revision:{revision_id:'a-r1',generation:1},messages:[]});await new Promise(resolve=>setTimeout(resolve,0));
      inspector.close();check(document.activeElement===input,'关闭未恢复原入口焦点');
      inspector.dispose();panel.remove();
    })()`);
    console.log(`Context layout: ${results.length-failures.length}/${results.length} passed; ${menus.length} menu layouts and 4 browser actions passed; selection, pinned revisions, stale responses and focus passed; evidence: ${output}`);
    win.destroy();
    app.quit();
  }).catch(error => { console.error(error.stack || error); app.exit(1); });
}
