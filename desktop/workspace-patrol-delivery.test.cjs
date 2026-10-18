/* 生产工作台交付卡：普通待输入不冒充完成，验收结果可直达固定版本，草稿及常驻状态保持。 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (!process.versions.electron) {
  require("node:test")("workspace delivery is visible beside input and opens the accepted revision in Electron", () => {
    const env = { ...process.env, FOCUS_PATROL_TEST_USER_DATA: fs.mkdtempSync(path.join(os.tmpdir(), "focus-delivery-")), FOCUS_TEST_HOLD_BOOTSTRAP: "1" };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 45000, windowsHide: true });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /PATROL_DELIVERY_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_PATROL_TEST_USER_DATA);
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 1000, webPreferences: {
      contextIsolation: false, sandbox: false, backgroundThrottling: false,
      preload: path.join(__dirname, "workspace-patrol-test-preload.cjs"),
    } });
    const run = code => win.webContents.executeJavaScript(code, true);
    const until = async code => {
      const deadline = Date.now() + 8000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${code}`);
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    };
    await win.loadFile(path.join(__dirname, "index.html"));
    await until("patrolFixture.releaseBootstrap");
    await run("patrolFixture.releaseBootstrap()");
    await until("state.tasks.length > 0");
    await run(`
      patrolFixture.seedHistory('delivered', []);
      window.deliveryState = {interaction_mode:'workspace_patrol',status:'waiting_user',health:'idle',waiting_reason:'awaiting_input',current_round_id:'done',goal_revision:1};
      window.deliveryReads = [];
      window.fileFeedbackFetch = async input => {
        const url = new URL(String(input), location.origin);
        if(url.pathname==='/desktop/api/workspaces/delivered') return Response.json({workspace_id:'delivered',display_name:'导入工具',path:'C:/work/importer'});
        if(url.pathname==='/desktop/api/agent-loops/loop-delivered/live') return Response.json({loop_id:'loop-delivered',last_sequence:0,loop:{entity_id:'loop-delivered',revision:1,updated_sequence:1,state:deliveryState},contexts:{root:{entity_id:'root',revision:1,updated_sequence:1,state:{title:'导入报告',current_revision_id:'r-new'}}}});
        if(url.pathname.endsWith('/loop-delivered/contexts/root/conversation')) {
          deliveryReads.push(url.searchParams.get('revision_id'));
          return Response.json({revision:{revision_id:url.searchParams.get('revision_id')},messages:[{message:{role:'assistant',content:'导入已修复，报告已生成。'}}],total:1,has_more:false});
        }
        return null;
      };
      openTaskNotification({taskId:'root',workspaceId:'delivered',loopId:'loop-delivered',deliveryId:'v-delivered'});
    `);
    await until("patrolFixture.connected('loop-delivered')");
    assert.equal(await run("document.querySelector('[data-patrol-delivery]').hidden"), true);
    await run(`
      document.querySelector('[data-patrol-content]').value='接下来调整报告格式';
      document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input',{bubbles:true}));
      deliveryState.final_result={summary:'修复导入并生成报告',round_id:'done',goal_revision:1,
        verification:{verification_id:'v-delivered',conclusion:'satisfied',criteria:[{check_id:'import',status:'satisfied',explanation:'导入回归测试通过'},{check_id:'optional',status:'unknown',explanation:'可选平台尚未验证'}],unresolved:['optional']},
        final_path:[{context_id:'root',revision_id:'r-delivered',title:'导入报告'}]};
      patrolFixture.emit('loop-delivered','loop','loop-delivered',2,deliveryState);
    `);
    await until("!document.querySelector('[data-patrol-delivery]').hidden");
    assert.equal(await run("document.querySelector('.workspace-patrol').classList.contains('is-quiet')"), true);
    assert.match(await run("document.querySelector('[data-patrol-state]').textContent"), /本次工作已交付/);
    assert.equal(await run("document.querySelector('[data-patrol-content]').value"), "接下来调整报告格式");
    assert.equal(await run("document.querySelector('[data-patrol-control=pause]').disabled"), false);
    await run("document.querySelector('[data-patrol-delivery] details').open=true");
    assert.match(await run("document.querySelector('[data-patrol-delivery]').textContent"), /验收通过 1 项 · 仍未解决 1 项/);
    for (const width of [1280, 440]) {
      win.setSize(width, 1000);
      await run("document.fonts.ready");
      await run("Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))");
      await run("document.querySelector('[data-patrol-delivery]').scrollIntoView({block:'end'})");
      await run("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");
      assert.equal(await run("document.querySelector('[data-patrol-delivery]').scrollWidth <= document.querySelector('[data-patrol-delivery]').clientWidth + 1"), true);
      if (process.env.FOCUS_PATROL_CAPTURE_DIR) {
        fs.mkdirSync(process.env.FOCUS_PATROL_CAPTURE_DIR, { recursive: true });
        fs.writeFileSync(path.join(process.env.FOCUS_PATROL_CAPTURE_DIR, `workspace-delivery-${width}.png`), (await win.webContents.capturePage()).toPNG());
      }
    }
    await run("document.querySelector('[data-patrol-delivery-context]').click()");
    await until("deliveryReads.length > 0");
    assert.equal(await run("deliveryReads.at(-1)"), "r-delivered");
    win.setSize(1280, 1000);
    await run("Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))");
    await run("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");
    assert.equal(await run("(() => {const card=document.querySelector('[data-patrol-delivery]').getBoundingClientRect(),input=document.querySelector('[data-patrol-composer]').getBoundingClientRect();return Math.abs(card.left-input.left)<2 && card.top>=input.bottom;})()"), true, "详情页交付卡必须跟随输入区，不能自动排入侧栏");
    await run("deliveryState.current_round_id='discussion';patrolFixture.emit('loop-delivered','loop','loop-delivered',3,deliveryState)");
    await until("document.querySelector('[data-patrol-state]').textContent.includes('准备接收新输入')");
    assert.match(await run("document.querySelector('[data-patrol-delivery]').textContent"), /最近一次已验收交付/);
    console.log("PATROL_DELIVERY_PASS");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
