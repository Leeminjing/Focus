/* 装配范围的真实桌面 API 集成验收；模型采样由 Python 测试控制。 */
const { app, BrowserWindow } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
app.setPath('userData', process.env.FOCUS_ASSEMBLY_USER_DATA);
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1440, height: 1000, webPreferences: {
    contextIsolation: false, sandbox: false, backgroundThrottling: false, offscreen: true,
    preload: path.join(__dirname, '../fixtures/assembly-scopes-preload.cjs'),
  } });
  const run = async code => {
    try { return await win.webContents.executeJavaScript(code, true); }
    catch (error) { throw Error(`Page expression failed: ${code}`, { cause: error }); }
  };
  win.webContents.on('console-message', (_event, level, message) => { if (level >= 3) console.error(message); });
  const until = async code => {
    const deadline = Date.now() + 20000;
    while (!await run(`Boolean(${code})`)) {
      if (Date.now() > deadline) throw Error(`Timeout: ${code} · ${await run('document.querySelector("#globalStatus")?.textContent')}`);
      await new Promise(resolve => setTimeout(resolve, 30));
    }
  };
  const shot = async name => {
    await new Promise(resolve => setTimeout(resolve, 250));
    win.webContents.invalidate();
    await new Promise(resolve => setTimeout(resolve, 100));
    await run('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    fs.writeFileSync(path.join(process.env.FOCUS_ASSEMBLY_EVIDENCE, name + '.png'), (await win.webContents.capturePage()).toPNG());
  };
  try {
    await win.loadURL(process.env.FOCUS_TASK_REAL_URL + '/desktop/');
    await until('state.tasks.length && document.querySelector("[data-action=show-assembly]")');
    await run('document.querySelector("[data-action=show-assembly]").click()');
    await until('activeTask()?.assembly_target?.scope === "global" && document.querySelector("[data-assembly-target]")');
    const globalId = await run('state.activeTaskId');
    await run(`document.querySelector('[data-assembly-target]').value=${JSON.stringify(process.env.FOCUS_ASSEMBLY_WORKSPACE_A)}; document.querySelector('[data-assembly-target]').dispatchEvent(new Event('change',{bubbles:true}))`);
    await until(`activeTask()?.assembly_target?.workspace_id === ${JSON.stringify(process.env.FOCUS_ASSEMBLY_WORKSPACE_A)} && !document.querySelector('[data-assembly-target]').disabled`);
    await until('state.skillCatalogs.get(state.activeTaskId)?.some(item=>item.name==="shared")');
    assert.equal(await run('state.skillCatalogs.get(state.activeTaskId).find(item=>item.name==="shared").description'), 'only A');
    await run('document.querySelector("[data-action=refresh-capabilities]").click()');
    await until('document.querySelector("#globalStatus").textContent.includes("已刷新")');
    await shot('assembly-workspace');
    await run('document.querySelector("[data-assembly-target]").value=""; document.querySelector("[data-assembly-target]").dispatchEvent(new Event("change",{bubbles:true}))');
    await until(`state.activeTaskId === ${JSON.stringify(globalId)} && activeTask()?.assembly_target?.scope === 'global'`);
    await until('state.skillCatalogs.get(state.activeTaskId)?.some(item=>item.name==="shared")');
    assert.equal(await run('state.skillCatalogs.get(state.activeTaskId).find(item=>item.name==="shared").description'), 'global shared');
    await run('document.querySelector("[data-action=pick-assembly-workspace]").click()');
    await until(`activeTask()?.assembly_target?.workspace_id === ${JSON.stringify(process.env.FOCUS_ASSEMBLY_WORKSPACE_B)}`);
    await until('state.skillCatalogs.get(state.activeTaskId)?.some(item=>item.name==="shared")');
    assert.notEqual(await run('state.activeTaskId'), globalId);
    assert.equal(await run('state.skillCatalogs.get(state.activeTaskId).find(item=>item.name==="shared").description'), 'global shared');
    win.setSize(740, 950);
    await run('document.querySelector("[data-action=close-inspector]").click()');
    await until('!state.inspector.open');
    await shot('assembly-narrow');
    const bounds = await run('(()=>{const e=document.querySelector("[data-assembly-target]").getBoundingClientRect();return {left:e.left,right:e.right,width:innerWidth}})()');
    assert.ok(bounds.left >= 0 && bounds.right <= bounds.width);
    console.log('ASSEMBLY_SCOPES_UI_PASS');
  } finally { win.destroy(); app.quit(); }
}).catch(error => { console.error(error.stack); app.exit(1); });
