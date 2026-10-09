/* 本文件对外提供隐藏 Electron 中两种真实页面的隔离验收。
 * 输入为新 userData 目录、生产 index.html 与离线 API fixture；输出为分栏选择、搜索/绑定/迟到响应、首次四类输入、连续草稿、折叠和观测断言。
 * 工作流为无可见窗口加载完整应用，操作真实 DOM 与单路 Live；普通会话继续展示原 Agent 消息。
 * 示例：node --test desktop/workspace-patrol-page.test.cjs。没有外部 Provider、生产库或安装应用写入。
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
if (!process.versions.electron) {
  require("node:test")("Patrol and ordinary pages in hidden Electron", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-ui-"));
    const env = { ...process.env, FOCUS_PATROL_TEST_USER_DATA: directory };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", timeout: 90000, windowsHide: true });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /PATROL_UI_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_PATROL_TEST_USER_DATA);
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 900, webPreferences: { contextIsolation: false, sandbox: false, preload: path.join(__dirname, "workspace-patrol-test-preload.cjs") } });
    const errors = [];
    win.webContents.on("console-message", (_event, _level, message) => { if (message?.includes("Uncaught")) errors.push(message); });
    await win.loadFile(path.join(__dirname, "index.html"));
    const run = async expression => {
      try { return await win.webContents.executeJavaScript(expression, true); }
      catch (error) { throw new Error(`Page expression failed: ${expression}`, { cause: error }); }
    };
    const until = async expression => {
      const end = Date.now() + 8000;
      while (!await run(expression)) { if (Date.now() > end) throw new Error(`Timeout: ${expression}`); await new Promise(resolve => setTimeout(resolve, 20)); }
    };
    await until('document.body.dataset.view === "focus" && document.querySelector(".conversation-message, .message")');
    const ordinary = await run('document.querySelector("#app").textContent');
    assert.ok(ordinary.length > 100);
    const types = await run('window.patrolFixture.workspaces');
    const dense = [{ workspace_id: "focus", display_name: "Focus", path: "C:\\Users\\brubing\\Desktop\\ag-project\\focus" },
      { workspace_id: "duplicate-a", display_name: "workspace", path: "C:/first/same-name" },
      { workspace_id: "duplicate-b", display_name: "workspace", path: "C:/second/same-name/unique-tail" },
      { workspace_id: "escaped", display_name: '<img src=x onerror="window.unsafePicker=true">', path: 'C:/<unsafe>/"quoted"' },
      ...Array.from({ length: 100 }, (_, i) => ({ workspace_id: `dense-${i}`, display_name: `workspace ${i}`, path: `C:/temporary/${"long-directory/".repeat(8)}${i}` }))];
    await run(`window.patrolFixture.workspaces = ${JSON.stringify(dense)}; document.querySelector("[data-action=show-patrol]").click()`);
    await until('document.querySelectorAll("[data-patrol-workspace-id]").length === 104');
    assert.equal(await run('document.querySelector("[data-patrol-workspace-detail] h2").textContent'), "Focus");
    const layout = await run('(() => { const list=document.querySelector("[data-patrol-workspaces]"), detail=document.querySelector("[data-patrol-workspace-detail]"), button=document.querySelector("[data-patrol-enter]"); return {scrollable:list.scrollHeight>list.clientHeight, separate:detail.getBoundingClientRect().left>list.getBoundingClientRect().right, enter:button.getBoundingClientRect().bottom<=innerHeight, overflow:document.documentElement.scrollWidth>innerWidth}; })()');
    assert.deepEqual(layout, { scrollable: true, separate: true, enter: true, overflow: false });
    await run('document.querySelector("[data-patrol-workspace-id=duplicate-b]").click()');
    assert.ok((await run('document.querySelector(".patrol-workspace-path").textContent')).includes("second/same-name"));
    assert.equal(await run('window.patrolFixture.submissions.length'), 0);
    assert.equal(await run('Boolean(document.querySelector("[data-patrol-content]"))'), false);
    await run('document.querySelector("[data-patrol-workspace-id=escaped]").click()');
    assert.equal(await run('Boolean(window.unsafePicker) || Boolean(document.querySelector("[data-patrol-workspace-detail] img"))'), false);
    await run('const search=document.querySelector("[data-patrol-search]"); search.focus(); search.value="SECOND\\\\SAME-NAME\\\\UNIQUE-TAIL"; search.dispatchEvent(new Event("input"))');
    assert.equal(await run('document.querySelectorAll("[data-patrol-workspace-id]").length'), 1);
    assert.equal(await run('document.activeElement.hasAttribute("data-patrol-search")'), true);
    await run('render()');
    assert.equal(await run('document.querySelectorAll("[data-patrol-workspace-id]").length'), 1);
    assert.equal(await run('document.activeElement.hasAttribute("data-patrol-search")'), true);
    await run('document.querySelector("[data-patrol-search]").value="not-a-workspace"; document.querySelector("[data-patrol-search]").dispatchEvent(new Event("input"))');
    assert.ok((await run('document.querySelector("[data-patrol-workspaces]").textContent')).includes("没有匹配"));
    assert.equal(await run('Boolean(document.querySelector("[data-patrol-enter]"))'), false);
    await run('document.querySelector("[data-patrol-search]").value=""; document.querySelector("[data-patrol-search]").dispatchEvent(new Event("input"))');
    win.setContentSize(720, 800);
    const narrow = await run('(() => {const list=document.querySelector("[data-patrol-workspaces]").getBoundingClientRect(), detail=document.querySelector("[data-patrol-workspace-detail]").getBoundingClientRect(); return {stacked:detail.top>=list.bottom, overflow:document.documentElement.scrollWidth>innerWidth};})()');
    assert.deepEqual(narrow, { stacked: true, overflow: false });
    win.setContentSize(1280, 900);
    await run('document.querySelector("[data-patrol-bind]").click()');
    await until('!document.querySelector("[data-patrol-bind]").disabled');
    assert.equal(await run('window.patrolFixture.workspaceBinds.length'), 0);
    await run('window.patrolFixture.folderError=true; document.querySelector("[data-patrol-bind]").click()');
    await until('!document.querySelector("[data-patrol-binding-error]").hidden');
    assert.ok((await run('document.querySelector("[data-patrol-binding-error]").textContent')).includes("文件夹选择失败"));
    await run('window.patrolFixture.folderError=false; window.patrolFixture.folder="C:/bound-folder"; document.querySelector("[data-patrol-bind]").click()');
    await until('document.querySelector("[data-patrol-content]")');
    assert.deepEqual(await run('window.patrolFixture.workspaceBinds'), [{ path: "C:/bound-folder" }]);
    assert.equal(await run('window.patrolFixture.submissions.length'), 0);
    await run('window.patrolFixture.workspaces=[]; document.querySelector("[data-action=show-patrol]").click()');
    await until('document.querySelector("[data-patrol-workspaces]").getAttribute("aria-busy")==="false"');
    assert.ok((await run('document.querySelector("[data-patrol-workspaces]").textContent')).includes("还没有工作区"));
    await run(`window.patrolFixture.workspaces=${JSON.stringify(types)}; window.patrolFixture.workspaceError=true; document.querySelector("[data-action=show-patrol]").click()`);
    await until('!document.querySelector("[data-patrol-binding-error]").hidden');
    assert.equal(await run('document.querySelector("[data-patrol-bind]").disabled'), false);
    await run('window.patrolFixture.workspaceError=false; window.patrolFixture.holdWorkspaces=true; document.querySelector("[data-action=show-patrol]").click()');
    await until('Boolean(window.patrolFixture.releaseWorkspaces)');
    await run('document.querySelector("[data-action=focus-home]").click(); window.patrolFixture.releaseWorkspaces(); window.patrolFixture.holdWorkspaces=false');
    await until('document.body.dataset.view === "focus"');
    assert.equal(await run('Boolean(document.querySelector(".patrol-workspace-picker"))'), false);
    await run('localStorage.removeItem("focus-patrol-workspace")');
    for (const input_type of ["information", "outcome", "boundary", "completion_check"]) {
      await run('document.querySelector("[data-action=show-patrol]").click()');
      await until('document.querySelector("[data-patrol-workspaces] button")');
      await run(`document.querySelector('[data-patrol-workspace-id="${input_type}"]').click()`);
      await run('document.querySelector("[data-patrol-enter]").click()');
      await until('document.querySelector("[data-patrol-content]")');
      await run(`document.querySelector("[data-patrol-type]").value = ${JSON.stringify(input_type)}; document.querySelector("[data-patrol-type]").dispatchEvent(new Event("change")); document.querySelector("[data-patrol-content]").value="首条模糊信息"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()`);
      await until('document.querySelector("[data-patrol-history]").textContent.includes("发送成功")');
      const body = await run('window.patrolFixture.submissions.at(-1)');
      assert.equal(body.input_type, input_type);
      assert.ok(!body.context_id && !body.mode);
    }
    await run('window.patrolFixture.hold = true; document.querySelector("[data-patrol-content]").value="A"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit(); document.querySelector("[data-patrol-content]").value="B"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit(); document.querySelector("[data-patrol-content]").value="C 草稿"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input"))');
    await until('window.patrolFixture.releases.length === 2');
    await run('window.patrolFixture.releases[1](); window.patrolFixture.releases[0](); window.patrolFixture.hold=false');
    await until('Array.from(document.querySelectorAll("[data-patrol-history] article")).every(row => row.textContent.includes("发送成功"))');
    assert.equal(await run('document.querySelector("[data-patrol-content]").value'), "C 草稿");
    await run('document.querySelector("[data-patrol-composer]").requestSubmit()');
    await until('window.patrolFixture.submissions.at(-1).content === "C 草稿"');
    assert.equal(await run('document.querySelectorAll("[data-patrol-history] article").length'), 3);
    await run('document.querySelector("[data-patrol-fold]").click()');
    assert.equal(await run('document.querySelectorAll("[data-patrol-history] article").length'), 4);
    await run('window.patrolFixture.emit("loop-completion_check", "loop_wait_request", "question", 1, { status:"open", scope:{information_only:true}, prompt:"使用密码还是验证码？" })');
    await until('document.querySelector("[data-patrol-answer]")');
    await run('document.querySelector("[data-patrol-answer]").click(); document.querySelector("[data-patrol-content]").value="用验证码"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-composer]").requestSubmit()');
    await until('window.patrolFixture.submissions.at(-1).request_id === "question"');
    assert.equal(await run('Boolean(document.querySelector("[data-patrol-answer]"))'), true);
    await run('window.patrolFixture.lineage = { roots:{root:"r1"}, nodes:[{context_id:"root",revision_id:"r1",generation:1}],edges:[],complete:true }; window.patrolFixture.emit("loop-completion_check", "context", "root", 1, {current_revision_id:"r1",title:"首线程"})');
    await until('document.querySelector("[data-patrol-lineage] [data-context-id=root]")');
    await run('document.querySelector("[data-patrol-content]").value="查看时保留草稿"; document.querySelector("[data-patrol-content]").dispatchEvent(new Event("input")); document.querySelector("[data-patrol-lineage] [data-context-id=root]").click()');
    await until('document.querySelector("[data-patrol-context-content]").textContent.includes("Context 执行结果")');
    assert.equal(await run('document.body.dataset.view'), "patrol");
    assert.equal(await run('document.querySelector("[data-patrol-content]").value'), "查看时保留草稿");
    await run('window.patrolFixture.emit("loop-completion_check", "task_progress", "loop-completion_check", 2, {generation:1, document:{items:[{description:"已确认本地保存",state:"in_progress",support:"asserted"}]}})');
    await until('document.querySelector("[data-patrol-progress]").textContent.includes("已确认本地保存")');
    assert.equal(await run('document.querySelector("[data-patrol-lineage]").textContent.includes("首线程")'), true);
    await run('window.patrolFixture.emit("loop-completion_check", "fact", "run-fact", 1, { kind:"run",title:"另一个 Context 已完成",summary:"真实 Run 事实",context_id:"other-context",status:"success" })');
    await until('document.querySelector("[data-patrol-facts]").textContent.includes("另一个 Context 已完成")');
    const composer = await run('(() => { const r = document.querySelector("[data-patrol-content]").getBoundingClientRect(); return {top:r.top,bottom:r.bottom,height:innerHeight}; })()');
    assert.ok(composer.top >= 0 && composer.bottom <= composer.height, JSON.stringify(composer));
    assert.equal(await run('Boolean(document.querySelector("[data-patrol-answer]"))'), true);
    await win.webContents.reload();
    await until('document.body.dataset.view === "patrol" && document.querySelectorAll("[data-patrol-history] article").length === 3');
    assert.equal(await run('document.querySelector("#appInspector").hidden'), true);
    assert.equal(await run('document.querySelector(".shell-resizer-inspector").getAttribute("aria-hidden")'), "true");
    assert.ok((await run('document.querySelector("[data-patrol-history]").textContent')).includes("用验证码"));
    await run('document.querySelector("[data-action=focus-home]").click()');
    await until('document.body.dataset.view === "focus"');
    assert.ok((await run('document.querySelector("#app").textContent')).length > 100);
    assert.deepEqual(errors, []);
    console.log("PATROL_UI_PASS: four first types, rapid inputs, draft, folding, requests, independent progress/facts, ordinary page");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
