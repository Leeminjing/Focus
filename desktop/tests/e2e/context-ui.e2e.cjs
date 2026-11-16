/*
 * 本文件以 Electron 加载真实桌面页面并验证 Context 与壳层交互。输入为 preload 提供的固定
 * 工作区，输出为显式任务/Context 次级入口、六导航、自动策展来源排序、会话滚动与草稿恢复、手工派生入口退役、窄屏 Inspector 和焦点归还断言。
 */
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { app, BrowserWindow } = require("electron");

const testUserData = path.join(os.tmpdir(), `focus-context-ui-${process.pid}`);
fs.mkdirSync(testUserData, { recursive: true });
app.setPath("userData", testUserData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

async function run() {
  const captureQa = process.argv.includes("--qa-screenshot");
  const window = new BrowserWindow({
    show: false,
    // Exercise navigation and conversation state before checking narrow layouts.
    width: 1600,
    height: captureQa ? 1000 : 800,
    webPreferences: {
      preload: path.join(__dirname, "../fixtures/context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
      offscreen: true,
    },
  });
  await window.loadFile(path.join(__dirname, "../../src/renderer/index.html"));
  const result = await window.webContents.executeJavaScript(`(async () => {
    window.__contextTestError = '';
    window.__contextTestClicks = [];
    document.addEventListener('click', event => { window.__contextTestClicks.push(event.target.closest?.('[data-action]')?.dataset.action || 'none'); }, true);
    window.addEventListener('error', event => { window.__contextTestError = String(event.error?.stack || event.message); });
    window.addEventListener('unhandledrejection', event => { window.__contextTestError = String(event.reason?.stack || event.reason); });
    const waitFor = async selector => {
      for (let count = 0; count < 100; count += 1) {
        const node = document.querySelector(selector);
        if (node) return node;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      throw new Error('等待元素超时：' + selector + '\\n状态：' + document.querySelector('#globalStatus')?.textContent + '\\n脚本：' + window.__contextTestError + '\\n点击：' + window.__contextTestClicks.join(',') + '\\n内部：' + state.view + '/' + state.activeTaskId + '\\n页面：' + document.querySelector('#app')?.innerText.slice(0, 500));
    };
    const failures = [];
    await waitFor('[data-action="show-map"]');
    document.querySelector('[data-action="focus-home"]').click();
    await waitFor('.focus-view');
    document.querySelector('[data-action="show-contexts"]').click();
    const rail = await waitFor('.context-rail');
    const navItems = [...document.querySelectorAll('.app-nav-item[data-nav-key]')];
    if (navItems.length !== 6 || navItems.some(item => !item.querySelector('.ui-icon'))) failures.push('六项导航或图标未按新布局呈现');
    if (navItems.some(item => item.querySelector('.app-nav-label')?.getBoundingClientRect().width < 12)) failures.push('左侧导航文字未完整显示');
    const railOrder = [...rail.querySelectorAll('.context-rail-card')].map(card => card.dataset.taskId);
    if (railOrder.slice(0, 5).join(',') !== 'root,child,merged,blocked,sibling') failures.push('Focus 页没有按树展示分支与合并 Context：' + railOrder.join(','));
    if (rail.querySelector('[data-task-id="foreign-root"], [data-task-id="foreign-child"]')) failures.push('Context rail 错误混入同工作区的其他线程');
    if (rail.querySelector('.context-rail-card[aria-current="true"]')?.dataset.taskId !== 'child') failures.push('当前 Context 没有选中态');
    if (rail.querySelector('[data-action="derive-context"], [data-action="edit-context-definition"]')) failures.push('Context rail 仍提供已退役的手工派生或编辑入口');
    if (!rail.querySelector('.context-rail-card[data-task-id="blocked"].is-blocked')) failures.push('受阻 Context 没有可见状态');
    if (!rail.querySelector('.context-rail-item[data-context-id="merged"] .context-rail-parents')?.textContent.includes('安全 Context')) failures.push('多父 Context 没有附加父来源');
    if (rail.querySelector('.context-tree-root, .context-tree-children, .context-current-marker, .context-rail-lineage')) failures.push('Context rail 又引入了树线、层级胶囊或重复父来源');
    if (rail.querySelector('.context-family, .context-root-label, .context-derived-list, .context-derived-heading')) failures.push('Context rail 仍包含额外根容器、眉题或派生分区');
    const rootItem = rail.querySelector('.context-rail-card[data-task-id="root"]')?.closest('.context-rail-item');
    const childItem = rail.querySelector('.context-rail-card[data-task-id="child"]')?.closest('.context-rail-item');
    const mergedItem = rail.querySelector('.context-rail-card[data-task-id="merged"]')?.closest('.context-rail-item');
    const rootCardStyle = getComputedStyle(rootItem?.querySelector('.context-rail-card'));
    const childCardStyle = getComputedStyle(childItem?.querySelector('.context-rail-card'));
    const childCardRect = childItem?.querySelector('.context-rail-card')?.getBoundingClientRect();
    const childTitleRect = childItem?.querySelector('.context-rail-title')?.getBoundingClientRect();
    if (rootItem?.querySelector('.context-rail-card')?.textContent.includes('根 Context')) failures.push('根 Context 导航仍展示技术类型');
    if (childItem?.querySelector('.context-rail-card')?.textContent.includes('派生 Context')) failures.push('派生 Context 导航仍展示技术类型');
    if (!(parseFloat(getComputedStyle(childItem).marginLeft) > parseFloat(getComputedStyle(rootItem).marginLeft))) failures.push('一级派生没有保留 depth 缩进');
    if (!(parseFloat(getComputedStyle(mergedItem).marginLeft) > parseFloat(getComputedStyle(childItem).marginLeft))) failures.push('深层派生没有保留轻量缩进');
    if (parseFloat(rootCardStyle.borderTopWidth) < 0.5 || parseFloat(childCardStyle.borderTopWidth) < 0.5 || parseFloat(childCardStyle.borderTopLeftRadius) < 3) failures.push('Context 卡片缺少清晰完整边框或圆角');
    if (childCardStyle.borderTopColor === rootCardStyle.borderTopColor) failures.push('当前项完整蓝色边框与默认卡片无法区分');
    if (!['left', 'start'].includes(childCardStyle.textAlign)) failures.push('Context 卡片内容没有恢复左对齐');
    if (childTitleRect.left - childCardRect.left > 22) failures.push('公共 button 样式仍把 Context 卡片内容居中');
    const childDiagnostics = childItem?.querySelector('.context-rail-diagnostics');
    const rootDiagnostics = rootItem?.querySelector('.context-rail-diagnostics');
    if (!childDiagnostics?.textContent.includes('25%') || childDiagnostics.open) failures.push('缓存命中率未放入默认折叠的技术详情');
    if (!rootDiagnostics?.textContent.includes('—') || rootDiagnostics.open) failures.push('未知缓存状态未放入默认折叠的技术详情');
    const railList = rail.querySelector('.context-rail-list');
    const conversation = document.querySelector('#conversation');
    const conversationBeforeRailScroll = conversation.scrollTop;
    railList.scrollTop = railList.scrollHeight;
    if (!(railList.scrollTop > 0)) failures.push('Context rail 未形成独立滚动');
    if (conversation.scrollTop !== conversationBeforeRailScroll) failures.push('滚动 Context rail 改变了会话滚动位置');
    document.querySelector('#mainInput').value = 'child-draft';
    conversation.scrollTop = Math.min(160, conversation.scrollHeight - conversation.clientHeight);
    const childScroll = conversation.scrollTop;
    rail.querySelector('.context-rail-card[data-task-id="root"]').click();
    await waitFor('.focus-view[data-task-id="root"]');
    if (!document.querySelector('.context-rail-card[data-task-id="child"]')) failures.push('进入根 Context 后子 Context 消失');
    document.querySelector('#mainInput').value = 'root-draft';
    document.querySelector('.context-rail-card[data-task-id="child"]').click();
    await waitFor('.focus-view[data-task-id="child"]');
    if (document.querySelector('#mainInput').value !== 'child-draft') failures.push('切回 Context 后输入草稿没有恢复');
    if (document.querySelector('#conversation').scrollTop !== childScroll) failures.push('切回 Context 后会话滚动位置没有恢复');
    document.querySelector('[data-action="show-map"]').click();
    (await waitFor('[data-action="set-map-view"][data-map-view="cards"]')).click();
    await waitFor('.task-card');
    if (!document.querySelector('.map-workspace-group') || document.querySelectorAll('.map-root-group').length < 2) failures.push('全图未按工作区与根 Context 分组');
    const order = [...document.querySelectorAll('.task-card')].map(card => card.dataset.taskId);
    if (order.slice(0, 5).join(',') !== 'root,child,merged,blocked,sibling') failures.push('全图没有按 Context tree 排序：' + order.join(','));
    if (!order.includes('foreign-root') || !order.includes('foreign-child')) failures.push('全图错误隐藏了其他线程');
    const identities = [...document.querySelectorAll('.context-identity')].map(node => node.textContent.trim());
    if (!identities.some(text => text.startsWith('根 Context')) || identities.filter(text => text.startsWith('派生 Context')).length < 4) failures.push('Context 卡片缺少根/派生标识');
    const childShell = document.querySelector('.task-card[data-task-id="child"]').closest('.task-card-shell');
    if (childShell.querySelector('.task-card').textContent.includes('another-very-long-directory-name')) failures.push('长路径仍占据全图主扫描层级');
    if (!childShell.querySelector('.task-technical')?.textContent.includes('another-very-long-directory-name')) failures.push('折叠技术详情没有保留完整长路径');
    if (document.querySelector('[data-action="derive-context"], [data-action="edit-context-definition"]')) failures.push('全图仍提供普通 Context 手工写入口');
    document.querySelector('.task-card[data-task-id="blocked"]').click();
    await waitFor('.focus-view[data-task-id="blocked"]');
    if (document.querySelector('.context-editor-view, .context-decision')) failures.push('受阻 Context 导航仍打开旧编辑审批工作台');
    document.querySelector('[data-action="show-map"]').click();
    await waitFor('.task-card[data-task-id="child"]');
    document.querySelector('.task-card[data-task-id="child"]').click();
    await waitFor('.focus-view[data-task-id="child"]');
    return { failures };
  })()`);
  if (result.failures.length) throw new Error(result.failures.join("\n"));
  window.setSize(900, 680);
  const responsive = await window.webContents.executeJavaScript(`(async () => {
    await goFocusHome();
    await new Promise(resolve => setTimeout(resolve, 120));
    const failures = [];
    if (document.documentElement.scrollWidth > document.documentElement.clientWidth) failures.push('900×680 存在页面级横向滚动');
    const nav = document.querySelector('.app-navigation');
    if (nav.getBoundingClientRect().width > 124) failures.push('900×680 文字导航过宽');
    if ([...nav.querySelectorAll('.app-nav-label')].some(label => label.getBoundingClientRect().width < 12)) failures.push('900×680 导航文字被隐藏');
    const trigger = document.querySelector('[data-action="show-contexts"]');
    trigger.closest('details').open = true;
    trigger.focus();
    trigger.click();
    await new Promise(resolve => requestAnimationFrame(resolve));
    const inspector = document.querySelector('#appInspector');
    const rect = inspector.getBoundingClientRect();
    if (getComputedStyle(inspector).position !== 'absolute' || rect.right > innerWidth + 1 || rect.left < nav.getBoundingClientRect().right - 1) failures.push('900×680 Inspector 未形成导航后的覆盖层');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    for (let count = 0; count < 40 && !inspector.hidden; count += 1) await new Promise(resolve => setTimeout(resolve, 20));
    if (!inspector.hidden || document.activeElement !== trigger) failures.push('Inspector Escape 关闭后未归还焦点：' + JSON.stringify({ hidden: inspector.hidden, connected: trigger.isConnected, active: document.activeElement?.outerHTML?.slice(0, 160), returnFocus: state.inspector.returnFocus?.outerHTML?.slice(0, 160) }));
    return failures;
  })()`);
  if (responsive.length) throw new Error(responsive.join("\n"));
  const checkTaskWorkspaceLayout = async ({ available, inspector }) => {
    const workspace = document.querySelector('.app-workspace');
    workspace.style.maxWidth = available + 'px';
    const inspectorNode = document.querySelector('#appInspector');
    inspectorNode.hidden = !inspector;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const layout = document.querySelector('.task-workspace-layout');
    const list = layout.querySelector('.task-workspace-list');
    const chat = layout.querySelector('.focus-shell');
    const listRect = list.getBoundingClientRect();
    const chatRect = chat.getBoundingClientRect();
    const failures = [];
    const stacked = workspace.clientWidth <= 700;
    if ((chatRect.top >= listRect.bottom) !== stacked) failures.push('任务列表堆叠未跟随工作区宽度：' + workspace.clientWidth);
    if (!stacked && Math.abs(listRect.width - (workspace.clientWidth <= 1050 ? 160 : 240)) > 1) failures.push('任务列表栏宽未跟随工作区宽度');
    if (stacked) {
      const nav = list.querySelector('nav');
      if (getComputedStyle(nav).display !== 'flex' || listRect.height > 126) failures.push('窄工作区未形成紧凑的任务切换条：' + JSON.stringify({ available, inspector, width: workspace.clientWidth, display: getComputedStyle(nav).display, height: listRect.height }));
      nav.scrollLeft = nav.scrollWidth;
      if (nav.scrollWidth > nav.clientWidth && nav.scrollLeft === 0) failures.push('任务切换条无法横向滚动');
      nav.scrollLeft = 0;
      if (chatRect.width < layout.clientWidth - 1) failures.push('堆叠后聊天区未占满可用宽度');
    }
    if (inspector && (getComputedStyle(inspectorNode).position === 'absolute') !== (workspace.clientWidth <= 1050)) failures.push('检查器未按工作区宽度切为浮层');
    if (layout.scrollWidth > layout.clientWidth + 1) failures.push('任务工作区横向溢出');
    const composer = chat.querySelector('#mainInput');
    composer.value = '响应式切换保留草稿';
    workspace.style.maxWidth = (available + 1) + 'px';
    await new Promise(resolve => requestAnimationFrame(resolve));
    if (chat.querySelector('#mainInput') !== composer || composer.value !== '响应式切换保留草稿') failures.push('宽度切换丢失输入节点或草稿');
    return failures;
  };
  window.setContentSize(1680, 900);
  for (const available of [600, 680, 700, 701, 1050, 1051, 1200]) {
    for (const inspector of [false, true]) {
      const failures = await window.webContents.executeJavaScript(`(${checkTaskWorkspaceLayout})(${JSON.stringify({ available, inspector })})`);
      if (failures.length) throw new Error(failures.join("\n"));
    }
  }
  await window.webContents.executeJavaScript(`document.querySelector('.app-workspace').style.maxWidth = ''; document.querySelector('#appInspector').hidden = true`);
  if (captureQa) {
    await window.webContents.executeJavaScript(`(async () => {
      await goFocusHome();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    })()`);
    const screenshot = await window.webContents.capturePage();
    require("node:fs").writeFileSync(path.join(__dirname, "../../../design-qa-context-navigation.png"), screenshot.toPNG());
  }
  console.log("context UI checks passed");
  window.destroy();
}

app.whenReady().then(run).then(() => process.exit(0)).catch(error => {
  console.error(error.stack || error);
  process.exit(1);
});
