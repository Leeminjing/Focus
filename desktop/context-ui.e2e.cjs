/*
 * 本文件以 Electron 加载真实桌面页面并验证 Context 编辑交互。
 * 输入为 preload 提供的固定工作区，输出为树排序、拖拽语义和删除后滚动位置断言；
 * 示例：node_modules/electron/dist/electron.exe context-ui.e2e.cjs。
 */
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

async function run() {
  const captureQa = process.argv.includes("--qa-screenshot");
  const window = new BrowserWindow({
    show: false,
    width: captureQa ? 1600 : 1200,
    height: captureQa ? 1000 : 800,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
    },
  });
  await window.loadFile(path.join(__dirname, "index.html"));
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
    const rail = await waitFor('.context-rail');
    const railOrder = [...rail.querySelectorAll('.context-rail-card')].map(card => card.dataset.taskId);
    if (railOrder.slice(0, 5).join(',') !== 'root,child,merged,blocked,sibling') failures.push('Focus 页没有按树展示分支与合并 Context：' + railOrder.join(','));
    if (rail.querySelector('[data-task-id="foreign-root"], [data-task-id="foreign-child"]')) failures.push('Context rail 错误混入同工作区的其他线程');
    if (rail.querySelector('.context-rail-card[aria-current="true"]')?.dataset.taskId !== 'child') failures.push('当前 Context 没有选中态');
    if (!rail.querySelector('[data-action="derive-context"]')) failures.push('Context rail 缺少新增入口');
    if (!rail.querySelector('.context-rail-card[data-task-id="blocked"].is-blocked')) failures.push('受阻 Context 没有可见状态');
    if (!rail.querySelector('.context-rail-card[data-task-id="merged"] .context-rail-parents')?.textContent.includes('安全 Context')) failures.push('多父 Context 没有附加父来源');
    if (!rail.querySelector('[data-task-id="child"]')?.textContent.includes('缓存 25%')) failures.push('Context rail 没有展示实际缓存命中率');
    if (!rail.querySelector('[data-task-id="root"]')?.textContent.includes('缓存 —')) failures.push('无 usage 的 Context 没有展示未知缓存状态');
    if (!rail.querySelector('[data-action="edit-context-definition"][data-context-id="child"]')) failures.push('首次运行前的派生 Context 缺少编辑入口');
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
    await waitFor('.task-card');
    const order = [...document.querySelectorAll('.task-card')].map(card => card.dataset.taskId);
    if (order.slice(0, 5).join(',') !== 'root,child,merged,blocked,sibling') failures.push('全图没有按 Context tree 排序：' + order.join(','));
    if (!order.includes('foreign-root') || !order.includes('foreign-child')) failures.push('全图错误隐藏了其他线程');
    const identities = [...document.querySelectorAll('.context-identity')].map(node => node.textContent.trim());
    if (!identities.some(text => text.startsWith('根 Context')) || identities.filter(text => text.startsWith('派生 Context')).length < 4) failures.push('Context 卡片缺少根/派生标识');
    document.querySelector('.task-card[data-task-id="blocked"]').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await waitFor('.context-decision.blocked');
    if (document.querySelectorAll('.context-source-message').length !== 14) failures.push('受阻 Context 重新进入后没有恢复来源消息');
    if (document.querySelectorAll('.context-message-editor').length !== 1) failures.push('受阻 Context 重新进入后没有恢复 authored_messages');
    document.querySelector('[data-action="exit-context-editor"]').click();
    await waitFor('.task-card');
    document.querySelector('.task-card[data-task-id="child"]').click();
    await waitFor('[data-action="derive-context"]');
    document.querySelector('[data-action="edit-context-definition"][data-context-id="child"]').click();
    await waitFor('.context-definition-panel');
    if (contextDraftLocked()) failures.push('首次运行前重新打开的 Context 被错误锁定');
    if (state.contextDraft?.messages?.[0]?.content !== '尚未运行，可继续编辑') failures.push('首次运行前编辑没有恢复用户定义');
    if (!document.querySelector('[data-action="submit-context"]')) failures.push('首次运行前编辑缺少重新编译入口');
    document.querySelector('[data-action="exit-context-editor"]').click();
    await waitFor('.task-card[data-task-id="child"]');
    document.querySelector('.task-card[data-task-id="child"]').click();
    await waitFor('[data-action="derive-context"]');
    document.querySelector('[data-action="derive-context"]').click();
    let panel = await waitFor('.context-definition-panel');
    if (state.contextDraft?.sources?.[0]?.context_id !== 'child') failures.push('新增 Context 没有使用当前 Context 作为来源');
    let list = await waitFor('.context-message-list');
    const sourceList = await waitFor('.context-source-message-list');
    if (sourceList.querySelectorAll('.context-source-message').length !== 14) failures.push('左栏没有展示完整来源消息');
    if (state.contextDraft?.messages?.length !== 0 || list.querySelector('.context-message-editor')) failures.push('新 Context 右栏没有从空白开始');
    if (document.querySelector('[draggable="true"]')) failures.push('Context 编辑器仍使用浏览器默认 Drag and Drop');
    if (list.querySelector('[data-action="context-message-up"], [data-action="context-message-down"]')) failures.push('仍显示上移/下移按钮');
    const sourceHandle = sourceList.querySelector('[data-context-source-index="0"] [data-context-pointer-handle]');
    if (!sourceHandle) failures.push('来源消息缺少 Pointer 拖拽手柄');
    if (sourceHandle) {
      const start = sourceHandle.getBoundingClientRect();
      const target = list.getBoundingClientRect();
      sourceHandle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 7, button: 0, clientX: start.left + 5, clientY: start.top + 5 }));
      document.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 7, clientX: target.left + 30, clientY: target.top + 30 }));
      if (!document.querySelector('.context-drag-preview') || !list.querySelector('.context-drop-placeholder')) failures.push('Pointer 拖拽没有跟手预览或动态占位');
      document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 7, clientX: target.left + 30, clientY: target.top + 30 }));
      await new Promise(resolve => setTimeout(resolve, 220));
      list = document.querySelector('.context-message-list');
      if (state.contextDraft.messages.length !== 1 || state.contextDraft.messages[0]?.id !== 'message-0') failures.push('跨栏 drop 没有完整复制来源消息');
      if (JSON.stringify(state.contextDraft.messages[0]) !== JSON.stringify(state.contextDraft.sourceSnapshots[0].messages[0])) failures.push('跨栏 drop 丢失了高级协议字段');
      if (sourceList.querySelectorAll('.context-source-message').length !== 14) failures.push('跨栏 drop 修改了来源消息');
      if (list.querySelector('[data-context-message-json]')) failures.push('右栏消息默认永久展开 JSON');
      if (document.querySelector('.context-drag-preview, .context-drop-placeholder')) failures.push('拖拽结束后未清理临时视觉');
    }
    const firstCard = list.querySelector('.context-message-editor');
    firstCard.querySelector('[data-action="context-message-toggle"]').click();
    const sourceInput = await waitFor('[data-context-message-json]');
    const edited = JSON.parse(sourceInput.value);
    edited.content = '未失焦编辑必须保留';
    sourceInput.value = JSON.stringify(edited);
    const stableCard = list.querySelector('.context-message-editor');
    sourceList.querySelector('[data-context-source-index="1"] [data-action="context-source-copy"]').click();
    if (!stableCard.isConnected) failures.push('新增消息时重建了未受影响卡片');
    if (state.contextDraft.messages[0]?.content !== '未失焦编辑必须保留') failures.push('局部新增丢失未失焦 JSON 编辑');
    const draftHandle = stableCard.querySelector('[data-context-pointer-handle]');
    const draftStart = draftHandle.getBoundingClientRect();
    const reorderTarget = list.getBoundingClientRect();
    draftHandle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 8, button: 0, clientX: draftStart.left + 5, clientY: draftStart.top + 5 }));
    document.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 8, clientX: reorderTarget.left + 30, clientY: reorderTarget.bottom - 12 }));
    document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 8, clientX: reorderTarget.left + 30, clientY: reorderTarget.bottom - 12 }));
    await new Promise(resolve => setTimeout(resolve, 220));
    if (state.contextDraft.messages[1]?.content !== '未失焦编辑必须保留') failures.push('右栏 Pointer 重排顺序不正确');
    if (!stableCard.isConnected) failures.push('右栏重排替换了被移动卡片节点');
    const beforeCancel = state.contextDraft.messages.length;
    const cancelHandle = sourceList.querySelector('[data-context-source-index="2"] [data-context-pointer-handle]');
    const cancelStart = cancelHandle.getBoundingClientRect();
    cancelHandle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 9, button: 0, clientX: cancelStart.left + 5, clientY: cancelStart.top + 5 }));
    document.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 9, clientX: reorderTarget.left + 30, clientY: reorderTarget.top + 30 }));
    document.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 9 }));
    await new Promise(resolve => setTimeout(resolve, 180));
    if (state.contextDraft.messages.length !== beforeCancel || document.querySelector('.context-drag-preview, .context-drop-placeholder')) failures.push('取消拖拽改变了定义或遗留临时视觉');
    for (let index = 2; index < 12; index += 1) sourceList.querySelector('[data-context-source-index="' + index + '"] [data-action="context-source-copy"]').click();
    panel = document.querySelector('.context-definition-panel');
    panel.scrollTop = 0;
    const autoHandle = sourceList.querySelector('[data-context-source-index="12"] [data-context-pointer-handle]');
    const autoStart = autoHandle.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const longTarget = list.getBoundingClientRect();
    autoHandle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 10, button: 0, clientX: autoStart.left + 5, clientY: autoStart.top + 5 }));
    document.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 10, clientX: longTarget.left + 30, clientY: panelRect.bottom - 3 }));
    await new Promise(resolve => setTimeout(resolve, 100));
    const autoScrollTop = panel.scrollTop;
    document.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 10 }));
    await new Promise(resolve => setTimeout(resolve, 180));
    if (!(autoScrollTop > 0)) failures.push('拖拽靠近右栏边缘时没有自动滚动');
    panel.scrollTop = Math.min(900, panel.scrollHeight - panel.clientHeight);
    const before = panel.scrollTop;
    const survivor = document.querySelectorAll('.context-message-editor')[8];
    document.querySelectorAll('[data-action="context-message-delete"]')[4].click();
    await new Promise(resolve => setTimeout(resolve, 260));
    const after = document.querySelector('.context-definition-panel').scrollTop;
    if (!(before > 0)) failures.push('测试页面未形成可滚动长列表');
    if (after === 0) failures.push('删除消息后滚动位置归零');
    if (!survivor.isConnected) failures.push('删除消息时重建了未受影响卡片');
    const lengthAfterDelete = state.contextDraft.messages.length;
    const undo = await waitFor('[data-action="context-message-undo"]');
    undo.click();
    if (state.contextDraft.messages.length !== lengthAfterDelete + 1) failures.push('撤销没有恢复完整消息');
    sourceList.querySelector('[data-context-source-index="12"] [data-action="context-source-copy"]').click();
    sourceList.querySelector('[data-context-source-index="13"] [data-action="context-source-copy"]').click();
    const keys = [...document.querySelectorAll('.context-message-editor')].map(card => card.dataset.contextUiKey);
    if (new Set(keys).size !== keys.length || keys.some(key => !key)) failures.push('重复或缺失消息 id 破坏了 UI 身份：' + keys.join(','));
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = query => ({ matches: query.includes('prefers-reduced-motion'), media: query, addEventListener() {}, removeEventListener() {} });
    const reducedBefore = state.contextDraft.messages.length;
    [...document.querySelectorAll('[data-action="context-message-delete"]')].at(-1).click();
    if (state.contextDraft.messages.length !== reducedBefore - 1) failures.push('reduced-motion 下删除功能失效');
    document.querySelector('[data-action="context-message-undo"]').click();
    if (state.contextDraft.messages.length !== reducedBefore) failures.push('reduced-motion 下撤销功能失效');
    window.matchMedia = originalMatchMedia;
    return { failures, before, after };
  })()`);
  if (result.failures.length) throw new Error(`${result.failures.join("\n")}\nscrollTop: ${result.before} -> ${result.after}`);
  if (captureQa) {
    await window.webContents.executeJavaScript(`(async () => {
      state.view = 'context';
      renderContextEditor();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      document.querySelector('.context-definition-panel').scrollTop = 0;
      document.querySelector('.context-source-message-list').scrollTop = 0;
    })()`);
    const screenshot = await window.webContents.capturePage();
    require("node:fs").writeFileSync(path.join(__dirname, "..", "design-qa-context-composer.png"), screenshot.toPNG());
  }
  console.log("context UI checks passed");
  window.destroy();
}

app.whenReady().then(run).then(() => process.exit(0)).catch(error => {
  console.error(error.stack || error);
  process.exit(1);
});
