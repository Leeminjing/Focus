/* 验证任务/历史标题栏按内容区宽度换行，长标题和操作按钮不越界。
 * 示例：node desktop/tests/e2e/task-heading-layout.e2e.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
if (!process.versions.electron) {
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const result = require("node:child_process").spawnSync(require("electron"), [__filename], { env, encoding: "utf8", windowsHide: true, timeout: 30000 });
  process.stdout.write(result.stdout || ""); process.stderr.write(result.stderr || ""); process.exit(result.status ?? 1);
} else {
  const { app, BrowserWindow } = require("electron");
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-task-heading-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 900, webPreferences: { offscreen: true, backgroundThrottling: false } });
    const source = file => fs.readFileSync(path.join(__dirname, "../../src/renderer/shared/styles", file), "utf8");
    const styles = ["tokens.css", "base.css", "components.css", "views.css"].map(source).join("\n");
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>${styles}body{margin:0;padding:24px}main{width:420px}</style><main><header class="task-page-heading"><div><small>测试工作区</small><h1>任务</h1><p>一条任务，一条持续会话。</p></div><div id="actions"></div></header></main>`));
    const run = code => win.webContents.executeJavaScript(code);
    for (const viewport of [1440, 800]) {
      win.setContentSize(viewport, 900);
      for (const width of [280, 420, 560, 1000]) {
        for (const title of ["任务", "实现长任务标题与完整历史记录".repeat(4), "unbroken_task_title_".repeat(12)]) {
          const result = await run(`(async()=>{
            const host=document.querySelector('main'),header=host.firstElementChild;
            host.style.width='${width}px';header.querySelector('h1').textContent=${JSON.stringify(title)};
            document.querySelector('#actions').innerHTML=['回到 Patrol','＋ 新建任务','返回历史查找','继续任务'].map(text=>'<button>'+text+'</button>').join('');
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            const heading=header.firstElementChild.getBoundingClientRect(),actions=header.lastElementChild.getBoundingClientRect(),bounds=header.getBoundingClientRect();
            const buttons=[...header.querySelectorAll('button')];
            return {viewport:innerWidth,width:host.clientWidth,scrollWidth:host.scrollWidth,
              titleWidth:heading.width,actionsBelow:actions.top>=heading.bottom,
              noOverlap:actions.top>=heading.bottom||actions.left>=heading.right,
              contained:[header.firstElementChild,header.lastElementChild,...buttons].every(node=>{const r=node.getBoundingClientRect();return r.left>=bounds.left-1&&r.right<=bounds.right+1&&r.bottom<=bounds.bottom+1}),
              readable:buttons.every(button=>button.clientHeight>=28&&button.scrollWidth<=button.clientWidth+1)};
          })()`);
          const label=JSON.stringify({title,...result});
          assert.equal(result.scrollWidth,result.width,label);
          assert.ok(result.noOverlap&&result.contained&&result.readable,label);
          if(width<=560)assert.ok(result.actionsBelow,label);
          if(width===1000)assert.equal(result.actionsBelow,false,label);
        }
      }
    }
    // 全图/历史查找也使用同一标题栏，但只有一个直接子按钮。
    assert.ok(await run(`(()=>{const header=document.querySelector('header');header.lastElementChild.outerHTML='<button>回到 Patrol</button>';document.querySelector('main').style.width='280px';return header.scrollWidth<=header.clientWidth})()`));
    console.log("Task heading layout: PASS (24 long-title/action layouts and direct action button)");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
