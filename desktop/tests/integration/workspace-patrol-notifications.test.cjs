"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (!process.versions.electron) {
  require("node:test")("Patrol notification locates a wait after delayed Live loading in hidden Electron", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-notifications-"));
    const env = { ...process.env, FOCUS_PATROL_TEST_USER_DATA: directory, FOCUS_TEST_HOLD_BOOTSTRAP: "1" };
    delete env.ELECTRON_RUN_AS_NODE;
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], {
      env, encoding: "utf8", timeout: 30000, windowsHide: true,
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /PATROL_NOTIFICATIONS_PASS/);
  });
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", process.env.FOCUS_PATROL_TEST_USER_DATA);
  app.disableHardwareAcceleration();
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 900, webPreferences: {
      contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
      preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"),
    } });
    const run = code => win.webContents.executeJavaScript(code, true);
    const until = async code => {
      const deadline = Date.now() + 5000;
      while (!await run(`Boolean(${code})`)) {
        if (Date.now() > deadline) throw new Error(`Timeout: ${code}`);
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    };
    await win.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
    await until("patrolFixture.releaseBootstrap");
    await run("patrolFixture.releaseBootstrap()");
    await until("state.tasks.length > 0");
    await run(`
      patrolFixture.seedHistory("notify", []);
      window.fileFeedbackFetch = async input => {
        const url = new URL(String(input), location.origin);
        if (url.pathname === "/desktop/api/tasks") return Response.json([{task_id:"notify-root", lifecycle:"active",
          workspace_id:"notify", loop:{loop_id:"loop-notify", interaction_mode:"workspace_patrol", workspace_id:"notify", initial_context_id:"notify-root"}}]);
        if (url.pathname === "/desktop/api/workspaces/notify") return Response.json({workspace_id:"notify",path:"C:/notify",display_name:"Notify"});
        if (url.pathname === "/desktop/api/agent-loops/loop-notify/live") {
          await new Promise(resolve => { window.releaseNotificationLive = resolve; });
          return Response.json({loop_id:"loop-notify",last_sequence:0,
            loop:{entity_id:"loop-notify",revision:1,updated_sequence:1,state:{status:"waiting_user",interaction_mode:"workspace_patrol"}},
            wait_requests:{question:{entity_id:"question",revision:1,updated_sequence:1,
              state:{status:"open",kind:"clarification",response_mode:"text",prompt:"请补充目标",response_contract:{max_length:200},scope:{}}},
              budget:{entity_id:"budget",revision:1,updated_sequence:1,
              state:{status:"open",kind:"budget_action",response_mode:"action",prompt:"已达到预算边界",
                response_contract:{actions:[{action:"revise_budget",label:"调整预算",input_schema:{max_rounds:7}}]},scope:{}}}}});
        }
        return null;
      };
      openTaskNotification({taskId:"notify-root",workspaceId:"notify",loopId:"loop-notify",attentionId:"budget"});
    `);
    await until('releaseNotificationLive && document.querySelector("[data-patrol-dialog]")?.open');
    assert.equal(await run("state.patrolWorkspace.workspace_id"), "notify");
    assert.equal(await run("state.view"), "patrol");
    await run("releaseNotificationLive()");
    await until('document.querySelector("[data-wait-request-id=budget] input")');
    await until('!document.querySelector("[data-patrol-dialog]").getAnimations().length');
    assert.equal(await run('document.activeElement === document.querySelector("[data-wait-request-id=budget] input")'), true);
    await run('document.activeElement.value="12"; document.activeElement.dispatchEvent(new Event("input",{bubbles:true})); document.querySelector("[data-patrol-dialog-close]").click()');
    await until('!document.querySelector("[data-patrol-dialog]").open');
    await run('workspacePatrolController.showAttention("budget","old-patrol")');
    assert.equal(await run('document.querySelector("[data-patrol-dialog]").open'), false);
    await run('openTaskNotification({taskId:"notify-root",workspaceId:"notify",loopId:"loop-notify",attentionId:"budget"})');
    await until('document.querySelector("[data-patrol-dialog]").open && !document.querySelector("[data-patrol-dialog]").getAnimations().length');
    assert.equal(await run('document.querySelector("[data-wait-request-id=budget] input").value'), "12");
    assert.equal(await run('document.activeElement === document.querySelector("[data-wait-request-id=budget] input")'), true);
    await run('openTaskNotification({taskId:"notify-root",workspaceId:"notify",loopId:"loop-notify",attentionId:"question"})');
    assert.equal(await run('document.activeElement === document.querySelector("[data-wait-request-id=question] textarea")'), true);
    console.log("PATROL_NOTIFICATIONS_PASS");
    win.destroy();
    app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
