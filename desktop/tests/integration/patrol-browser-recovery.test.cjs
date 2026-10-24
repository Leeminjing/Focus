/* 隐藏 Chromium 使用真实 IndexedDB，刷新页面后恢复完整输入及稳定重试。
 * 示例：node --test desktop/tests/integration/patrol-browser-recovery.test.cjs。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
if (!process.versions.electron) {
  require("node:test")("browser IndexedDB survives reload with drafts, attachments and exact failed payload", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-browser-recovery-"));
    try {
      const env = { ...process.env, FOCUS_RECOVERY_TEST_DATA: directory };
      delete env.ELECTRON_RUN_AS_NODE;
      const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 45000, windowsHide: true });
      assert.equal(result.status, 0, result.stdout + result.stderr);
      assert.match(result.stdout, /BROWSER_RECOVERY_PASS/);
    } finally {
      assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_RECOVERY_TEST_DATA);
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, webPreferences: { backgroundThrottling: false } });
    const load = () => win.loadFile(path.join(__dirname, "../../src/renderer/desktop-patrol.html"));
    const run = expression => win.webContents.executeJavaScript(expression);
    await load();
    const setup = `
      window.repository = await FocusPatrolBrowserRecovery.open('workbench', {byteLimit:1024});
      window.posts = [];
      window.uploads = [];
      window.intake = FocusWorkspacePatrolIntake.create({runtime:{}, recovery:repository,
        limits:{general_file_bytes:1024,original_image_bytes:512,run_material_count:4},
        fetchImpl:async (url,options) => { uploads.push(await options.body.get('file').text()); return Response.json({material_id:'material',sha256:'digest'}); },
        api:{workspaceInputs:async () => ({items:[],next_before:null}), submitWorkspaceInput:async (_id,body) => {posts.push(body);throw new Error('lost receipt');}}
      });`;
    const original = await run(`(async () => { ${setup}
      intake.remember({workspace_id:'a',path:'/a'});
      intake.store('a').edit({content:'failed content',file_refs:[{result_key:'fixed-file'}]});
      await intake.add('a',[{kind:'text',text:'failed bytes'}]);
      try {await intake.submit('a');} catch {}
      intake.store('a').edit({content:'new draft',request_id:'q',request_revision:3,budget_preset:{mode:'test'}});
      await intake.add('a',[{kind:'file',name:'binary',bytes:new Uint8Array([0,255,128])}]);
      intake.store('b').edit({content:'workspace b'});
      await intake.flush();
      const sameRepository = FocusWorkspacePatrolIntake.create({runtime:{},api:{},recovery:repository});
      if (sameRepository.store('a').get().draft.content !== 'new draft') throw new Error('same repository returned stale state');
      const panel = await FocusPatrolBrowserRecovery.open('panel');
      const isolated = panel.read(); panel.close();
      repository.close();
      return {draft:intake.store('a').get().draft,payload:posts[0],isolated};
    })()`);
    assert.equal(original.isolated.state, null);
    await load();
    const restored = await run(`(async () => { ${setup}
      const draft=intake.store('a').get().draft;
      const failed=intake.store('a').get().rows[0];
      const result={draft, bytes:Array.from(intake.source(draft.attachments[0].local_id)),failed,binding:intake.readBinding(),other:intake.store('b').get().draft.content,postsBefore:posts.length};
      try {await intake.retry('a',failed.submission_id);} catch {}
      result.payload=posts[0]; result.uploads=uploads;
      await intake.flush(); repository.close(); return result;
    })()`);
    assert.deepEqual(restored.draft, original.draft);
    assert.deepEqual(restored.bytes, [0,255,128]);
    assert.equal(restored.failed.status, "failed");
    assert.equal(restored.other, "workspace b");
    assert.equal(restored.binding.workspace_id, "a");
    assert.equal(restored.postsBefore, 0);
    assert.deepEqual(restored.payload, original.payload);
    assert.deepEqual(restored.uploads, []);
    const errors = await run(`(async () => {
      const repo=await FocusPatrolBrowserRecovery.open('limits',{byteLimit:4});
      const state={version:1,workspaces:[],source_ids:['a']};
      await repo.put('a',new Uint8Array([0,255,1])); await repo.save(state);
      let quota=false, missing=false, unavailable=false;
      try {await repo.put('b',new Uint8Array(2));} catch {quota=true;}
      try {await repo.save({...state,source_ids:['missing']});} catch {missing=true;}
      try {await FocusPatrolBrowserRecovery.open('none',{indexedDB:null});} catch {unavailable=true;}
      await repo.put('next',new Uint8Array([4]));
      await repo.save(state); // 排队的旧快照不能删除尚未首次被引用的材料。
      await repo.save({...state,source_ids:['next']});
      await repo.save({...state,source_ids:[]}); repo.close();
      const reopened=await FocusPatrolBrowserRecovery.open('limits',{byteLimit:4});
      const result={quota,missing,unavailable,state:reopened.read().state,sources:reopened.read().sources};
      const oldWrite=reopened.save({...state,source_ids:[]});
      reopened.checkpoint({...state,source_ids:[],binding:{workspace_id:'checkpoint',path:'/latest'}});
      await oldWrite;reopened.close();
      const latest=await FocusPatrolBrowserRecovery.open('limits',{byteLimit:4});
      result.checkpoint=latest.read().state.binding.workspace_id;latest.close();return result;
    })()`);
    assert.equal(errors.quota && errors.missing && errors.unavailable, true);
    assert.deepEqual(errors.sources, []);
    assert.deepEqual(errors.state.source_ids, []);
    assert.equal(errors.checkpoint,"checkpoint");
    console.log("BROWSER_RECOVERY_PASS");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
