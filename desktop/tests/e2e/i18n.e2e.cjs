/* 使用隔离 Electron/HTTP fixture 验证真实页面英语、语言往返切换和中文草稿保留。 */
const { app, BrowserWindow } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "focus-i18n-")));
app.on("window-all-closed", () => {});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
app.whenReady().then(async () => {
  const window = new BrowserWindow({ show: false, width: 1440, height: 1000, webPreferences: {
    preload: path.join(__dirname, "../fixtures/workspace-patrol-test-preload.cjs"), contextIsolation: false, sandbox: false,
  } });
  const run = code => window.webContents.executeJavaScript(code, true);
  const until = async code => {
    const end = Date.now() + 10000;
    while (!await run(`Boolean(${code})`)) {
      if (Date.now() > end) throw new Error(`Timeout: ${code}`);
      await delay(30);
    }
  };
  const chinese = () => run(`(() => {
    const residual=[];
    const visit=node=>{
      if(node.nodeType===1 && (['SCRIPT','STYLE','TEMPLATE'].includes(node.tagName) || node.hidden || node.closest('[hidden]')))return;
      if(node.nodeType===3 && /\\p{Script=Han}/u.test(node.textContent))residual.push(node.textContent.trim());
      if(node.nodeType===1) for(const key of ['aria-label','placeholder','title','alt'])if(/\\p{Script=Han}/u.test(node.getAttribute(key)||''))residual.push(key+': '+node.getAttribute(key));
      for(const child of node.childNodes)visit(child);
    };visit(document.body);return residual;
  })()`);
  await window.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
  await until('document.querySelector("[data-patrol-content]")');
  await until('state.tasks.length > 0');
  await run(`state.tasks.forEach(task=>{task.title='Task';task.workspace_name='Workspace';task.workspace_path='C:/workspace'})`);
  await run(`FocusI18n.setLocale('en-US')`);
  await delay(50);
  assert.deepEqual(await chinese(), [], "Patrol picker must be fully English");
  await run(`state.patrolWorkspace={workspace_id:'w',display_name:'Workspace'};render();document.querySelector('[data-patrol-content]').value='保留中文草稿';document.querySelector('[data-patrol-content]').dispatchEvent(new Event('input',{bubbles:true}))`);
  await run(`FocusI18n.setLocale('zh-CN');FocusI18n.setLocale('en-US')`);
  await delay(50);
  assert.equal(await run(`document.querySelector('[data-patrol-content]').value`), "保留中文草稿");
  assert.deepEqual(await chinese(), [], "Patrol workbench must be fully English");
  await run(`modelSettings.snapshot=null;modelSettings.loading=null;const baseApi=api;api=(path,options)=>path==='/desktop/api/settings/models'?Promise.resolve({models:[],shipped_models:[],editable_fields:[],adapters:[],curation_output_methods:[],setup_required:false}):baseApi(path,options);undefined`);
  await run(`openSettings()`);
  assert.deepEqual(await chinese(), [], "Settings must be fully English");
  await run(`selectSettingsTab('models')`);
  await delay(50);
  assert.deepEqual(await chinese(), [], "Model settings must be fully English");
  await run(`settingsDialog.close();state.tasks=[{task_id:'english',title:'Task',workspace_id:'w',workspace_name:'Workspace',workspace_path:'C:/workspace',thread_id:'thread'}];state.activeTaskId='english';state.details.set('english',{messages:[],ui_state:{}});state.contextTrees.set('w',[]);state.view='focus';render()`);
  assert.deepEqual(await chinese(), [], "Task conversation controls must be fully English");
  for (const view of ["plugins", "memory", "map"]) {
    await run(`state.view=${JSON.stringify(view)};render()`);
    await delay(30);
    assert.deepEqual(await chinese(), [], `${view} must be fully English`);
  }
  await window.loadFile(path.join(__dirname, "../../src/renderer/desktop-patrol.html"), { query: { surface: "panel" } });
  await until('document.querySelector("#panel") && !document.querySelector("#panel").inert');
  await delay(50);
  await run(`FocusI18n.setLocale('zh-CN');document.querySelector('#instruction').value='中文草稿';document.querySelector('#instruction').dispatchEvent(new Event('input'));FocusI18n.setLocale('en-US')`);
  assert.equal(await run(`document.querySelector('#instruction').value`), "中文草稿");
  assert.deepEqual(await chinese(), [], "Desktop panel must be fully English");
  await run(`FocusI18n.setLocale('zh-CN');patrolFixture.folder='C:/EnglishWorkspace';document.querySelector('#binding').click()`);
  await until(`document.querySelector('#binding-name').textContent==='EnglishWorkspace'`);
  await until(`document.querySelector('[data-budget-preset]') && !document.querySelector('[data-budget-preset]').disabled`);
  await run(`FocusI18n.setLocale('en-US')`);
  assert.deepEqual(await chinese(), [], "Bound desktop panel and budget must refresh to English");
  await run(`document.querySelector('#open-files').click()`);
  await delay(30);
  assert.deepEqual(await chinese(), [], "File drawer must be fully English");
  await run(`FocusI18n.setLocale('zh-CN');const preview=document.createElement('section');preview.id='i18nPreview';document.body.append(preview);FocusFilePreview.mount(preview,{name:'sample.txt',target:{path:'sample.txt',current:true},text:'中文文件内容'})`);
  await run(`FocusI18n.setLocale('en-US')`);
  const previewText = await run(`document.querySelector('#i18nPreview').textContent`);
  assert.match(previewText, /中文文件内容/);
  assert.doesNotMatch(previewText.replaceAll("中文文件内容", ""), /\p{Script=Han}/u);
  console.log("I18N_E2E_PASS");
  window.destroy(); app.exit(0);
}).catch(error => { console.error(error); app.exit(1); });
