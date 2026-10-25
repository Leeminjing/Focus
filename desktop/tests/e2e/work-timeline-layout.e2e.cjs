/* 用生产工作台 DOM 与事件验证时间线正文高度、独立滚动和响应式面板切换。
 * 示例：node desktop/tests/e2e/work-timeline-layout.e2e.cjs。
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
  app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-timeline-layout-")));
  app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1440, height: 720, webPreferences: { offscreen: true, backgroundThrottling: false } });
    const View = require("../../src/renderer/features/patrol/workspace/workspace-patrol-view.js");
    const Timeline = require("../../src/renderer/features/files/work-timeline.js");
    const source = file => fs.readFileSync(path.join(__dirname, "../../src/renderer", file), "utf8");
    const styles = ["shared/styles/tokens.css", "shared/styles/base.css", "shared/styles/components.css", "shared/styles/workspace-patrol.css", "features/patrol/workspace/workspace-patrol-feedback.css"].map(source).join("\n");
    const rows = Array.from({ length: 30 }, (_, i) => Timeline.entryHtml({ entry_id: `entry-${i}`, category: "fact", title: `验证结果 ${i}`, summary: "已提交工作证据，可查看来源与版本。", recorded_at: "2026-10-25T04:00:00Z", context_ids: ["ctx"], references: {} })).join("");
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>${styles}body{margin:0;padding-top:60px}</style>${View.skeleton({ workspace_id: "layout", display_name: "测试工作区" })}`));
    const run = code => win.webContents.executeJavaScript(code);
    await run(`
      window.workspace=document.querySelector('.workspace-patrol');workspace.classList.remove('is-quiet');
      const details=document.querySelector('[data-patrol-details-content]');details.hidden=false;details.inert=false;
      document.querySelector('.patrol-quiet-heading').hidden=true;
      document.querySelector('[data-patrol-facts]').innerHTML=${JSON.stringify(rows)};
      window.contextHtml=${JSON.stringify(View.context({ context_id: "ctx", revision: { revision_id: "r1", generation: 1 } }, { context_id: "ctx", title: "测试与故障分析" }, "overview", { presentation: "workbench" }))};
    `);
    for (const [width, height] of [[1440, 720], [1440, 600], [1748, 904], [1000, 720], [800, 720]]) {
      win.setContentSize(width, height);
      const result = await run(`(async()=>{
        workspace.style.height=(innerHeight-60)+'px';workspace.dataset.workbenchPanel='facts';
        document.querySelector('[data-patrol-context-content]').innerHTML=contextHtml;
        for(const key of ['older','selected','clear'])document.querySelector('[data-timeline-'+key+']').hidden=false;
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        const card=document.querySelector('.patrol-facts-card'),stream=card.querySelector('[data-patrol-facts]');
        const rect=card.getBoundingClientRect(),body=stream.getBoundingClientRect(),selected=document.querySelector('.patrol-selected-card').getBoundingClientRect();
        stream.scrollTop=200;const scrolled=stream.scrollTop>0;
        const controls=[...card.children].filter(node=>node!==stream&&node.getClientRects().length).every(node=>{const r=node.getBoundingClientRect();return r.top>=rect.top&&r.bottom<=rect.bottom+1});
        return {width:innerWidth,height:innerHeight,cardHeight:rect.height,bodyHeight:body.height,controls,scrolled,
          contained:body.top>=rect.top&&body.bottom<=rect.bottom,
          noOverlap:innerWidth<1200||rect.bottom<=selected.top,
          contextBodyHeight:innerWidth>=1200?document.querySelector('.patrol-selected-card .patrol-context-body').clientHeight:null,
          inWorkspace:rect.bottom<=workspace.getBoundingClientRect().bottom+1};
      })()`);
      assert.ok(result.bodyHeight >= 140, JSON.stringify(result));
      assert.ok(result.bodyHeight >= result.cardHeight * .45, JSON.stringify(result));
      assert.ok(result.controls && result.scrolled && result.contained && result.noOverlap && result.inWorkspace, JSON.stringify(result));
      if (width >= 1200) assert.ok(result.contextBodyHeight >= 30, JSON.stringify(result));
      if (width < 1200) {
        assert.equal(await run(`workspace.dataset.workbenchPanel='context';getComputedStyle(document.querySelector('.patrol-facts-card')).display`), "none");
        assert.equal(await run(`getComputedStyle(document.querySelector('.patrol-selected-card')).display`), "flex");
      }
      console.log(JSON.stringify(result));
      if (process.env.FOCUS_TIMELINE_LAYOUT_EVIDENCE && width >= 1200) {
        fs.mkdirSync(process.env.FOCUS_TIMELINE_LAYOUT_EVIDENCE, { recursive: true });
        fs.writeFileSync(path.join(process.env.FOCUS_TIMELINE_LAYOUT_EVIDENCE, `timeline-${width}-${height}.png`), (await win.webContents.capturePage()).toPNG());
      }
    }
    console.log("Work Timeline layout: PASS");
    win.destroy(); app.quit();
  }).catch(error => { console.error(error); app.exit(1); });
}
