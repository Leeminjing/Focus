/*
 * 本文件以真实 Electron 验证 F22 可感知交互升级。输入为确定性的导航、Inspector、reasoning、
 * pending/success/error ToolMessage 与本地图标状态，输出为选中态、事件序列几何、图标基线、
 * disclosure、reduced-motion、溢出和 QA 截图；工作流不调用后端或改变用户数据。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-f22-premium-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const qaDir = path.join(__dirname, "..", "openspec", "changes", "f22-premium-interaction-system", "qa", "after");
fs.mkdirSync(qaDir, { recursive: true });

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 1440,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 120; count += 1) {
      if (document.querySelector('.focus-view')) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('Focus F22 启动超时'));
  })`);
  win.showInactive();
  await new Promise(resolve => setTimeout(resolve, 80));

  const result = await win.webContents.executeJavaScript(`(async () => {
    state.view = 'focus';
    state.activeTaskId = 'child';
    state.inspector.open = true;
    state.inspector.tab = 'context';
    const detail = state.details.get('child');
    detail.messages = [
      { id: 'human', role: 'human', content: '从工具清单中选出四个代表性工具。' },
      { role: 'ai', content: '', reasoning_content: '先覆盖文件、搜索、终端和通用工具，再比较结果。', tool_calls: [
        { id: 'a', name: 'demo_echo', args: { text: 'Hello' } },
        { id: 'b', name: 'list_files', args: { path: '.' } },
        { id: 'c', name: 'web_search', args: { query: 'Focus agent framework' } },
        { id: 'd', name: 'bash', args: { command: 'pwd' } },
      ] },
      { role: 'tool', name: 'demo_echo', tool_call_id: 'a', status: 'success', content: 'Hello' },
      { role: 'tool', name: 'list_files', tool_call_id: 'b', status: 'success', content: '2 files' },
      { role: 'tool', name: 'web_search', tool_call_id: 'c', status: 'error', content: '暂时无法连接搜索服务' },
      { role: 'ai', content: '前三项已经形成可比较结果，终端调用仍在进行。' },
    ];
    render();
    await new Promise(resolve => setTimeout(resolve, 260));
    const sequence = document.querySelector('.conversation-event-sequence');
    const eventNodes = [...document.querySelectorAll('.conversation-event')];
    const summaries = eventNodes.map(node => node.querySelector('summary'));
    const icons = [...document.querySelectorAll('.conversation-event-mark .ui-icon')];
    const nav = document.querySelector('.app-nav-item[data-nav-key="contexts"]');
    const tab = document.querySelector('.inspector-tabs button[aria-selected="true"]');
    const sequenceStyle = sequence ? getComputedStyle(sequence) : null;
    const navStyle = nav ? getComputedStyle(nav) : null;
    const tabStyle = tab ? getComputedStyle(tab) : null;
    const firstPositions = summaries.map(item => item.getBoundingClientRect());
    const iconRects = icons.map(icon => ({ width: icon.getBoundingClientRect().width, height: icon.getBoundingClientRect().height }));
    const pendingNode = document.querySelector('.conversation-event[data-event-key="tool:d"]');
    detail.messages.push({ role: 'tool', name: 'bash', tool_call_id: 'd', status: 'success', content: '/workspace' });
    replaceConversation(activeTask(), detail.messages);
    const settledNode = document.querySelector('.conversation-event[data-event-key="tool:d"]');
    return {
      sequenceExists: Boolean(sequence),
      sequenceGap: sequenceStyle?.rowGap || '',
      eventCount: eventNodes.length,
      summaryHeights: firstPositions.map(rect => rect.height),
      visualGaps: firstPositions.slice(1).map((rect, index) => rect.top - firstPositions[index].bottom),
      iconCount: icons.length,
      iconRects,
      navShadow: navStyle?.boxShadow || '',
      navBackground: navStyle?.backgroundColor || '',
      navTransition: navStyle?.transitionDuration || '',
      navTransitionProperty: navStyle?.transitionProperty || '',
      navCurrent: nav?.getAttribute('aria-current') || '',
      navMatchesSelected: nav?.matches('.app-nav-item[aria-current="page"]') || false,
      selectedToken: navStyle?.getPropertyValue('--surface-selected') || '',
      tabShadow: tabStyle?.boxShadow || '',
      tabBackground: tabStyle?.backgroundColor || '',
      inspectorAnimation: getComputedStyle(document.querySelector('#appInspector')).animationDuration,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      unnamedButtons: [...document.querySelectorAll('button')].filter(button => !(button.textContent.trim() || button.getAttribute('aria-label') || button.title)).length,
      stableEventIdentity: pendingNode === settledNode,
      settledStatus: settledNode?.querySelector('.conversation-event-status')?.textContent.trim() || '',
    };
  })()`);

  await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
  const image = await win.webContents.capturePage();
  fs.writeFileSync(path.join(qaDir, "f22-conversation-1440x900.png"), image.toPNG());

  win.webContents.debugger.attach("1.3");
  await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  const reduced = await win.webContents.executeJavaScript(`(() => {
    const button = document.querySelector('.app-nav-item');
    const detail = document.querySelector('.conversation-event-detail');
    return {
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      buttonTransition: getComputedStyle(button).transitionDuration,
      detailAnimation: detail ? getComputedStyle(detail).animationDuration : '0s',
    };
  })()`);
  win.webContents.debugger.detach();
  win.hide();
  win.destroy();

  const failures = [];
  const seconds = value => String(value).split(',').map(item => item.trim()).filter(Boolean).map(item => item.endsWith('ms') ? parseFloat(item) / 1000 : parseFloat(item));
  if (!result.sequenceExists) failures.push("连续工具事件没有语义序列");
  if (parseFloat(result.sequenceGap) > 2.1) failures.push(`事件序列 gap 过大: ${result.sequenceGap}`);
  if (result.eventCount !== 5) failures.push(`逻辑事件数量错误: ${result.eventCount}`);
  if (result.summaryHeights.some(height => height < 28 || height > 32.5)) failures.push(`summary 命中区异常: ${result.summaryHeights.join(',')}`);
  if (result.visualGaps.some(gap => gap > 2.5)) failures.push(`ToolMessage 行间距过大: ${result.visualGaps.join(',')}`);
  if (result.iconCount !== result.eventCount || result.iconRects.some(rect => Math.abs(rect.width - 14) > 0.5 || Math.abs(rect.height - 14) > 0.5)) failures.push(`事件图标尺寸或数量错误: ${JSON.stringify(result.iconRects)}`);
  if (!result.stableEventIdentity || result.settledStatus !== "完成") failures.push(`pending 原位更新失败: stable=${result.stableEventIdentity} status=${result.settledStatus}`);
  if (result.navCurrent !== "page") failures.push(`全局导航当前项错误: ${result.navCurrent}`);
  if (result.navTransitionProperty.split(',').some(item => item.trim() === 'all')) failures.push(`导航仍使用 transition: all: ${result.navTransitionProperty}`);
  if (!seconds(result.navTransition).some(value => value > 0 && value <= .18) || seconds(result.navTransition).some(value => value > .24)) failures.push(`导航反馈时长不在令牌范围: ${result.navTransition}`);
  if (!seconds(result.inspectorAnimation).some(value => value > 0 && value <= .24)) failures.push(`Inspector 进入反馈未消费 motion token: ${result.inspectorAnimation}`);
  if (result.navShadow !== "none" || result.tabShadow !== "none") failures.push(`仍有选中半框: nav=${result.navShadow} tab=${result.tabShadow}`);
  if (/rgba?\(0, 0, 0, 0\)/.test(result.navBackground) || /rgba?\(0, 0, 0, 0\)/.test(result.tabBackground)) failures.push(`中性选中面缺失: nav=${result.navBackground} tab=${result.tabBackground} matches=${result.navMatchesSelected} token=${result.selectedToken}`);
  if (result.documentOverflow > 1 || result.unnamedButtons) failures.push(`溢出或无名控件: ${JSON.stringify(result)}`);
  if (!reduced.matches || !/^0(?:s|\.0+s)?(?:, 0s)*$/.test(reduced.buttonTransition) || !/^0(?:s|\.0+s)?$/.test(reduced.detailAnimation)) failures.push(`reduced-motion 未完全降级: ${JSON.stringify(reduced)}`);
  if (failures.length) throw new Error(`F22 premium audit:\n- ${failures.join("\n- ")}`);
  console.log("f22-premium-ui-e2e: 无半框、紧凑事件序列、本地图标、reduced-motion 与溢出通过");
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
