/*
 * 本文件以真实 Electron 验证会话 Patrol 小兵交互。输入为确定性 Patrol/API 数据与鼠标、
 * 触摸、键盘、斜向/变向拖动、缩放和任务切换操作，输出为动作锚点、状态资源、拖动边界、
 * 单次位置持久化、详情复用、点击穿透和恢复位置断言。示例：node desktop/patrol-avatar.e2e.cjs。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-patrol-avatar-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 1280,
    height: 840,
    webPreferences: {
      preload: path.join(__dirname, "patrol-avatar-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  win.webContents.on("preload-error", (_event, preloadPath, error) => {
    console.error(`preload-error ${preloadPath}: ${error.stack || error}`);
  });
  await win.loadFile(path.join(__dirname, "index.html"));
  try {
    await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
      for (let count = 0; count < 150; count += 1) {
        if (document.querySelectorAll('.patrol-avatar').length === 2) return resolve();
        await new Promise(next => setTimeout(next, 20));
      }
      reject(new Error('会话 Patrol 小兵启动超时'));
    })`);
  } catch (error) {
    const diagnostic = await win.webContents.executeJavaScript(`({
      hasModule: Boolean(window.FocusPatrolAvatar),
      hasTest: Boolean(window.__patrolAvatarTest),
      taskId: typeof state === 'undefined' ? null : state.activeTaskId,
      agents: typeof state === 'undefined' ? null : [...state.agents.entries()],
      view: typeof state === 'undefined' ? null : state.view,
      layer: Boolean(document.querySelector('.patrol-avatar-layer')),
      appText: document.querySelector('#app')?.textContent?.slice(0, 300),
    })`);
    throw new Error(`${error.message}: ${JSON.stringify(diagnostic)}`);
  }

  const initial = await win.webContents.executeJavaScript(`(() => {
    const avatars = [...document.querySelectorAll('.patrol-avatar')];
    const layer = document.querySelector('.patrol-avatar-layer');
    const first = avatars[0].getBoundingClientRect();
    const second = avatars[1].getBoundingClientRect();
    const images = avatars.map(item => item.querySelector('img').getAttribute('src'));
    return {
      count: avatars.length,
      ids: avatars.map(item => item.dataset.agentId),
      visuals: avatars.map(item => item.dataset.visual),
      images,
      distinct: Math.abs(first.left - second.left) > 20 || Math.abs(first.top - second.top) > 20,
      layerPointerEvents: getComputedStyle(layer).pointerEvents,
      buttonPointerEvents: getComputedStyle(avatars[0].querySelector('button')).pointerEvents,
      unnamed: avatars.filter(item => !item.querySelector('.patrol-avatar__button').getAttribute('aria-label')).length,
    };
  })()`);
  if (initial.count !== 2 || !initial.distinct || initial.unnamed) throw new Error(`初始多小兵失败: ${JSON.stringify(initial)}`);
  if (initial.visuals.join(",") !== "working,loading") throw new Error(`状态资源映射失败: ${initial.visuals}`);
  if (!initial.images.every(source => source.startsWith("./assets/patrol-avatar/"))) throw new Error(`资源路径异常: ${initial.images}`);
  if (initial.layerPointerEvents !== "none" || initial.buttonPointerEvents !== "auto") throw new Error(`点击穿透失败: ${JSON.stringify(initial)}`);

  const motionAnchors = await win.webContents.executeJavaScript(`(async () => {
    const names = ['move-left', 'move-right', 'rise', 'descend', 'rotate'];
    const anchors = [];
    for (const name of names) {
      const image = new Image();
      image.src = './assets/patrol-avatar/' + name + '.png';
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = 384;
      canvas.height = 384;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      context.drawImage(image, 0, 0, 384, 384);
      const pixels = context.getImageData(0, 0, 384, 384).data;
      let count = 0;
      let sumX = 0;
      let sumY = 0;
      for (let y = 100; y < 225; y += 1) {
        for (let x = 126; x < 260; x += 1) {
          const offset = (y * 384 + x) * 4;
          const alpha = pixels[offset + 3];
          if (alpha <= 200 || pixels[offset] >= 45 || pixels[offset + 1] >= 60 || pixels[offset + 2] >= 80) continue;
          count += 1;
          sumX += x;
          sumY += y;
        }
      }
      let leftEffect = 0;
      let rightEffect = 0;
      for (let y = 80; y < 240; y += 1) {
        for (let x = 0; x < 126; x += 1) leftEffect += pixels[(y * 384 + x) * 4 + 3] > 8 ? 1 : 0;
        for (let x = 260; x < 384; x += 1) rightEffect += pixels[(y * 384 + x) * 4 + 3] > 8 ? 1 : 0;
      }
      anchors.push({ name, x: sumX / count, y: sumY / count, count, leftEffect, rightEffect });
    }
    const xs = anchors.map(item => item.x);
    const ys = anchors.map(item => item.y);
    return { anchors, spreadX: Math.max(...xs) - Math.min(...xs), spreadY: Math.max(...ys) - Math.min(...ys) };
  })()`);
  if (motionAnchors.spreadX > 8 || motionAnchors.spreadY > 8) throw new Error(`动作资源主体锚点不一致: ${JSON.stringify(motionAnchors)}`);
  const leftMotion = motionAnchors.anchors.find(item => item.name === "move-left");
  const rightMotion = motionAnchors.anchors.find(item => item.name === "move-right");
  if (leftMotion.rightEffect <= leftMotion.leftEffect * 1.5 || rightMotion.leftEffect <= rightMotion.rightEffect * 1.5) {
    throw new Error(`左右移动拖尾方向错误: ${JSON.stringify({ leftMotion, rightMotion })}`);
  }

  const qaDirectory = path.join(__dirname, "..", "openspec", "changes", "smooth-session-patrol-drag", "qa");
  fs.mkdirSync(qaDirectory, { recursive: true });
  for (const name of ["move-left", "move-right", "rise", "descend", "rotate"]) {
    await win.webContents.executeJavaScript(`(async () => {
      const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
      const image = avatar.querySelector('.patrol-avatar__image');
      avatar.dataset.visual = '${name}';
      image.src = './assets/patrol-avatar/${name}.png';
      await image.decode();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    })()`);
    const capture = await win.webContents.capturePage();
    fs.writeFileSync(path.join(qaDirectory, `motion-${name}-1280x840.png`), capture.toPNG());
  }
  await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const image = avatar.querySelector('.patrol-avatar__image');
    avatar.dataset.visual = 'working';
    image.src = './assets/patrol-avatar/working.png';
    await image.decode();
  })()`);

  const details = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    trigger.click();
    const opened = !avatar.querySelector('.patrol-avatar__bubble').hidden && trigger.getAttribute('aria-expanded') === 'true';
    avatar.querySelector('.patrol-avatar__detail').click();
    for (let count = 0; count < 80 && state.agentDialog.messages.length !== 2; count += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    return {
      opened,
      agentId: state.agentDialog.agentId,
      history: state.agentDialog.messages.length,
      inspector: state.inspector.open && state.inspector.tab === 'agents',
    };
  })()`);
  if (!details.opened || details.agentId !== "patrol-running-0001" || details.history !== 2 || !details.inspector) {
    throw new Error(`详情复用失败: ${JSON.stringify(details)}`);
  }

  const streamed = await win.webContents.executeJavaScript(`(async () => {
    const source = window.__patrolAvatarTest.eventSources.find(item => item.url.includes('run-pending'));
    source.emit('metadata', { agent_id: 'patrol-pending-0002', data: { status: 'running' } });
    await new Promise(resolve => requestAnimationFrame(resolve));
    const runningVisual = document.querySelector('[data-agent-id="patrol-pending-0002"]').dataset.visual;
    window.__patrolAvatarTest.agents.child[1].latest_run.status = 'success';
    source.emit('end', { status: 'success', error: null });
    for (let count = 0; count < 100; count += 1) {
      const avatar = document.querySelector('[data-agent-id="patrol-pending-0002"]');
      if (avatar?.dataset.status === 'success') break;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const avatar = document.querySelector('[data-agent-id="patrol-pending-0002"]');
    return { runningVisual, terminalStatus: avatar?.dataset.status, terminalVisual: avatar?.dataset.visual };
  })()`);
  if (streamed.runningVisual !== "working" || streamed.terminalStatus !== "success" || streamed.terminalVisual !== "happy") {
    throw new Error(`SSE 状态同步失败: ${JSON.stringify(streamed)}`);
  }

  const drag = await win.webContents.executeJavaScript(`(async () => {
    const conversation = document.querySelector('.conversation');
    conversation.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 41, pointerType: 'mouse', isPrimary: true }));
    const input = document.querySelector('#mainInput');
    input.value = '这个输入不能被位置保存覆盖';
    conversation.scrollTop = 96;
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    const bubble = avatar.querySelector('.patrol-avatar__bubble');
    const before = avatar.getBoundingClientRect();
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 7, pointerType: 'touch', isPrimary: true, button: 0, clientX: before.left + 40, clientY: before.top + 40 }));
    trigger.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', isPrimary: true, buttons: 1, clientX: before.left + 230, clientY: before.top + 170 }));
    const movingVisual = avatar.dataset.visual;
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 7, pointerType: 'touch', isPrimary: true, button: 0, clientX: before.left + 230, clientY: before.top + 170 }));
    await new Promise(resolve => setTimeout(resolve, 80));
    const after = avatar.getBoundingClientRect();
    const saved = window.__patrolAvatarTest.savedBodies.at(-1)?.body;
    return {
      moved: Math.hypot(after.left - before.left, after.top - before.top),
      movingVisual,
      bubbleHidden: bubble.hidden,
      saved,
      savedCount: window.__patrolAvatarTest.savedBodies.length,
      transform: avatar.style.transform,
    };
  })()`);
  if (drag.moved < 80 || drag.movingVisual !== "rotate" || !drag.bubbleHidden) throw new Error(`触摸拖动失败: ${JSON.stringify(drag)}`);
  if (drag.savedCount !== 1 || drag.saved.input !== "这个输入不能被位置保存覆盖" || !drag.saved.patrol_avatar_positions?.["patrol-running-0001"]) {
    throw new Error(`位置单次合并保存失败: ${JSON.stringify(drag)}`);
  }

  const continuousDrag = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    const image = avatar.querySelector('.patrol-avatar__image');
    const before = avatar.getBoundingClientRect();
    const pointerId = 27;
    const savedBefore = window.__patrolAvatarTest.savedBodies.length;
    const sources = [];
    const observer = new MutationObserver(records => {
      records.forEach(record => {
        if (record.type === 'attributes' && record.attributeName === 'src') sources.push(image.getAttribute('src'));
      });
    });
    observer.observe(image, { attributes: true });
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId, pointerType: 'mouse', isPrimary: true, button: 0, buttons: 1, clientX: before.left + 40, clientY: before.top + 40 }));
    const positions = [];
    for (let index = 1; index <= 48; index += 1) {
      const clientX = before.left + 40 + index * 4;
      const clientY = before.top + 40 + (index % 2 ? 4 : -4);
      trigger.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId, pointerType: 'mouse', isPrimary: true, buttons: 1, clientX, clientY }));
      positions.push(avatar.getBoundingClientRect().left);
    }
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId, pointerType: 'mouse', isPrimary: true, button: 0, clientX: before.left + 232, clientY: before.top + 40 }));
    observer.disconnect();
    await new Promise(resolve => setTimeout(resolve, 80));
    return {
      sourceAssignments: sources.length,
      sourceTransitions: sources.filter((value, index) => index === 0 || value !== sources[index - 1]).length,
      movedContinuously: positions.every((value, index) => index === 0 || value >= positions[index - 1]),
      finalVisual: avatar.dataset.visual,
      finalSource: image.getAttribute('src'),
      savedDelta: window.__patrolAvatarTest.savedBodies.length - savedBefore,
    };
  })()`);
  if (!continuousDrag.movedContinuously || continuousDrag.sourceAssignments >= 24 || continuousDrag.sourceTransitions >= 24) {
    throw new Error(`持续拖动动作抖动: ${JSON.stringify(continuousDrag)}`);
  }
  if (continuousDrag.finalVisual !== "working" || !continuousDrag.finalSource.endsWith('/working.png') || continuousDrag.savedDelta !== 1) {
    throw new Error(`持续拖动结束状态异常: ${JSON.stringify(continuousDrag)}`);
  }

  const diagonalDrag = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    const before = avatar.getBoundingClientRect();
    const pointerId = 31;
    const savedBefore = window.__patrolAvatarTest.savedBodies.length;
    const startX = before.left + 40;
    const startY = before.top + 40;
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId, pointerType: 'mouse', isPrimary: true, button: 0, buttons: 1, clientX: startX, clientY: startY }));
    const visuals = [];
    for (const [x, y] of [[10, 8], [23, 15], [36, 22], [49, 27]]) {
      trigger.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId, pointerType: 'mouse', isPrimary: true, buttons: 1, clientX: startX + x, clientY: startY + y }));
      visuals.push(avatar.dataset.visual);
    }
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId, pointerType: 'mouse', isPrimary: true, button: 0, clientX: startX + 49, clientY: startY + 27 }));
    await new Promise(resolve => setTimeout(resolve, 80));
    return {
      visuals,
      finalVisual: avatar.dataset.visual,
      savedDelta: window.__patrolAvatarTest.savedBodies.length - savedBefore,
    };
  })()`);
  if (diagonalDrag.visuals.join(",") !== "rotate,rotate,rotate,move-right" || diagonalDrag.finalVisual !== "working" || diagonalDrag.savedDelta !== 1) {
    throw new Error(`斜向迟滞失败: ${JSON.stringify(diagonalDrag)}`);
  }

  const keyboard = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    const before = avatar.getBoundingClientRect();
    const savedBefore = window.__patrolAvatarTest.savedBodies.length;
    trigger.focus();
    trigger.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowRight', shiftKey: true }));
    await new Promise(resolve => setTimeout(resolve, 60));
    const after = avatar.getBoundingClientRect();
    return { delta: after.left - before.left, savedDelta: window.__patrolAvatarTest.savedBodies.length - savedBefore, active: document.activeElement === trigger };
  })()`);
  if (keyboard.delta < 30 || keyboard.savedDelta !== 1 || !keyboard.active) throw new Error(`键盘移动失败: ${JSON.stringify(keyboard)}`);

  const boundary = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-agent-id="patrol-running-0001"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    const before = avatar.getBoundingClientRect();
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 9, pointerType: 'mouse', isPrimary: true, button: 0, clientX: before.left + 30, clientY: before.top + 30 }));
    trigger.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 9, pointerType: 'mouse', isPrimary: true, buttons: 1, clientX: -2000, clientY: -2000 }));
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 9, pointerType: 'mouse', isPrimary: true, button: 0, clientX: -2000, clientY: -2000 }));
    await new Promise(resolve => setTimeout(resolve, 60));
    const avatarRect = avatar.getBoundingClientRect();
    const layerRect = document.querySelector('.patrol-avatar-layer').getBoundingClientRect();
    return { left: avatarRect.left - layerRect.left, top: avatarRect.top - layerRect.top, right: layerRect.right - avatarRect.right, bottom: layerRect.bottom - avatarRect.bottom };
  })()`);
  if (Math.min(boundary.left, boundary.top, boundary.right, boundary.bottom) < -1) throw new Error(`边界裁剪失败: ${JSON.stringify(boundary)}`);

  const restored = await win.webContents.executeJavaScript(`(async () => {
    const childPosition = state.details.get('child').ui_state.patrol_avatar_positions['patrol-running-0001'];
    state.activeTaskId = 'root';
    await hydrateActive('root');
    render();
    const rootIds = [...document.querySelectorAll('.patrol-avatar')].map(item => item.dataset.agentId);
    state.activeTaskId = 'child';
    await hydrateActive('child');
    render();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const recovered = state.details.get('child').ui_state.patrol_avatar_positions['patrol-running-0001'];
    return { rootIds, childPosition, recovered, childCount: document.querySelectorAll('.patrol-avatar').length };
  })()`);
  if (restored.rootIds.join(",") !== "patrol-success-0003" || restored.childCount !== 2 || JSON.stringify(restored.childPosition) !== JSON.stringify(restored.recovered)) {
    throw new Error(`任务隔离或恢复失败: ${JSON.stringify(restored)}`);
  }

  win.setSize(900, 680);
  await wait(120);
  const resized = await win.webContents.executeJavaScript(`(() => {
    const layer = document.querySelector('.patrol-avatar-layer').getBoundingClientRect();
    return [...document.querySelectorAll('.patrol-avatar')].map(item => {
      const rect = item.getBoundingClientRect();
      return { left: rect.left - layer.left, top: rect.top - layer.top, right: layer.right - rect.right, bottom: layer.bottom - rect.bottom };
    });
  })()`);
  if (resized.some(rect => Math.min(rect.left, rect.top, rect.right, rect.bottom) < -1)) throw new Error(`缩放恢复越界: ${JSON.stringify(resized)}`);

  win.setSize(1280, 840);
  await wait(120);
  const standby = await win.webContents.executeJavaScript(`(async () => {
    state.activeTaskId = 'empty';
    state.view = 'focus';
    state.agentDialog = { agentId: null, messages: [], busy: false };
    await hydrateActive('empty');
    render();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const avatar = document.querySelector('.patrol-avatar');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    trigger.click();
    const bubble = avatar.querySelector('.patrol-avatar__bubble');
    return {
      count: document.querySelectorAll('.patrol-avatar').length,
      avatarId: avatar.dataset.avatarId,
      hasAgentId: Object.hasOwn(avatar.dataset, 'agentId'),
      presence: avatar.dataset.presence,
      visual: avatar.dataset.visual,
      heading: bubble.querySelector('strong').textContent,
      status: bubble.querySelector('.patrol-avatar__status').textContent,
      message: bubble.querySelector('.patrol-avatar__message').textContent,
      action: bubble.querySelector('.patrol-avatar__detail').textContent,
      agentDialogId: state.agentDialog.agentId,
      backendAgents: window.__patrolAvatarTest.agents.empty.length,
    };
  })()`);
  if (standby.count !== 1 || standby.avatarId !== "__standby__" || standby.hasAgentId || standby.presence !== "standby" || standby.visual !== "idle") {
    throw new Error(`待命小兵常驻失败: ${JSON.stringify(standby)}`);
  }
  if (standby.heading !== "Patrol 小兵" || standby.status !== "待命" || !standby.message.includes("尚未布置任务") || standby.action !== "布置任务") {
    throw new Error(`待命气泡语义失败: ${JSON.stringify(standby)}`);
  }
  if (standby.agentDialogId || standby.backendAgents !== 0) throw new Error(`待命小兵污染真实 Agent: ${JSON.stringify(standby)}`);

  const capture = await win.webContents.capturePage();
  fs.writeFileSync(path.join(qaDirectory, "standby-patrol-1280x840.png"), capture.toPNG());

  const standbyMove = await win.webContents.executeJavaScript(`(async () => {
    const savedBefore = window.__patrolAvatarTest.savedBodies.length;
    const avatar = document.querySelector('[data-avatar-id="__standby__"]');
    const trigger = avatar.querySelector('.patrol-avatar__button');
    trigger.click();
    const before = avatar.getBoundingClientRect();
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 18, pointerType: 'mouse', isPrimary: true, button: 0, clientX: before.left + 30, clientY: before.top + 30 }));
    trigger.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 18, pointerType: 'mouse', isPrimary: true, buttons: 1, clientX: before.left + 170, clientY: before.top - 60 }));
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 18, pointerType: 'mouse', isPrimary: true, button: 0, clientX: before.left + 170, clientY: before.top - 60 }));
    await new Promise(resolve => setTimeout(resolve, 80));
    const after = avatar.getBoundingClientRect();
    const saved = window.__patrolAvatarTest.savedBodies.at(-1);
    return {
      before: { left: before.left, top: before.top },
      after: { left: after.left, top: after.top },
      savedDelta: window.__patrolAvatarTest.savedBodies.length - savedBefore,
      savedPath: saved?.path,
      savedBody: saved?.body,
      backendAgents: window.__patrolAvatarTest.agents.empty.length,
    };
  })()`);
  if (Math.hypot(standbyMove.after.left - standbyMove.before.left, standbyMove.after.top - standbyMove.before.top) < 80) {
    throw new Error(`待命小兵拖动失败: ${JSON.stringify(standbyMove)}`);
  }
  if (standbyMove.savedDelta !== 1 || standbyMove.savedPath !== "/desktop/api/tasks/empty/ui-state" || !standbyMove.savedBody?.patrol_avatar_positions?.__standby__) {
    throw new Error(`待命位置保存失败: ${JSON.stringify(standbyMove)}`);
  }
  if (standbyMove.backendAgents !== 0) throw new Error(`移动待命小兵创建了真实 Agent: ${JSON.stringify(standbyMove)}`);

  const draftEntry = await win.webContents.executeJavaScript(`(async () => {
    const avatar = document.querySelector('[data-avatar-id="__standby__"]');
    avatar.querySelector('.patrol-avatar__button').click();
    avatar.querySelector('.patrol-avatar__detail').click();
    for (let count = 0; count < 80 && state.view !== 'draft'; count += 1) await new Promise(resolve => setTimeout(resolve, 10));
    return { view: state.view, openCalls: [...window.__patrolAvatarTest.draftOpenCalls], agentDialogId: state.agentDialog.agentId };
  })()`);
  if (draftEntry.view !== "draft" || draftEntry.openCalls.at(-1) !== "empty" || draftEntry.agentDialogId) {
    throw new Error(`待命布置入口失败: ${JSON.stringify(draftEntry)}`);
  }

  const handoff = await win.webContents.executeJavaScript(`(async () => {
    document.querySelector('[data-action="exit-draft"]').click();
    for (let count = 0; count < 80 && state.view === 'draft'; count += 1) await new Promise(resolve => setTimeout(resolve, 10));
    state.view = 'focus';
    render();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const restored = document.querySelector('[data-avatar-id="__standby__"]');
    const before = restored.getBoundingClientRect();
    window.__patrolAvatarTest.agents.empty.push({
      agent_id: 'patrol-first-empty',
      checkpoint_ns: 'patrol:patrol-first-empty',
      permissions: ['read'],
      latest_run: { run_id: 'run-first-empty', task_id: 'empty', agent_id: 'patrol-first-empty', kind: 'patrol', status: 'pending' },
    });
    await hydrateActive('empty');
    render();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const real = document.querySelector('[data-agent-id="patrol-first-empty"]');
    const after = real.getBoundingClientRect();
    return {
      count: document.querySelectorAll('.patrol-avatar').length,
      standbyCount: document.querySelectorAll('[data-presence="standby"]').length,
      presence: real.dataset.presence,
      visual: real.dataset.visual,
      distance: Math.hypot(after.left - before.left, after.top - before.top),
      savedStandby: state.details.get('empty').ui_state.patrol_avatar_positions.__standby__,
    };
  })()`);
  if (handoff.count !== 1 || handoff.standbyCount !== 0 || handoff.presence !== "agent" || handoff.visual !== "loading" || handoff.distance > 2) {
    throw new Error(`首个真实 Patrol 原位接替失败: ${JSON.stringify(handoff)}`);
  }

  console.log("patrol-avatar-e2e: 常驻待命、多小兵、状态、拖动、键盘、详情、持久化、任务隔离与原位接替通过");
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
