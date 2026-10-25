/* 使用生产记忆整理视图与离线会话验证三/双/单栏、滚动可达性和编辑身份。
 * 示例：electron desktop/tests/e2e/memory-responsive.e2e.cjs。
 */
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { app, BrowserWindow } = require('electron');

app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'focus-memory-layout-')));
app.disableHardwareAcceleration();

const checkMemoryLayout = async width => {
  const compose = document.querySelector('.memory-compose');
  const grid = compose.querySelector('.memory-compose-grid');
  const panes = [...grid.children];
  const source = compose.querySelector('.memory-source-text');
  const title = compose.querySelector('[data-memory-field=title]');
  const content = compose.querySelector('[data-memory-field=content]');
  const titleValue = title.value;
  const contentValue = content.value;
  const draft = state.memory.draft;
  const selected = state.memory.activeMessageId;
  const text = source.textContent;
  compose.style.width = width + 'px';
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const available = compose.clientWidth;
  const columns = available <= 600 ? 1 : available <= 900 ? 2 : 3;
  const check = (value, message) => { if (!value) throw new Error(available + 'px: ' + message); };
  check(getComputedStyle(grid).gridTemplateColumns.split(' ').length === columns, 'source columns must follow compose width');
  const rects = panes.map(pane => pane.getBoundingClientRect());
  if (columns === 1) check(rects[1].top >= rects[0].bottom && rects[2].top >= rects[1].bottom, 'all source panes must stack');
  if (columns === 2) check(rects[0].top === rects[1].top && rects[2].top >= rects[0].bottom && rects[2].width > rects[0].width, 'original text must span the second row');
  if (columns === 3) check(rects.every(rect => rect.top === rects[0].top), 'wide source panes must share a row');
  for (const pane of panes) {
    check(pane.clientWidth > 0 && pane.clientHeight >= 180, 'source pane must retain reading space');
    pane.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const rect = pane.getBoundingClientRect();
    const host = compose.getBoundingClientRect();
    check(rect.top >= host.top - 1 && rect.bottom <= host.bottom + 1, 'source pane must be reachable by scrolling');
  }
  check(compose.scrollWidth <= compose.clientWidth + 1, 'compose must not overflow horizontally');
  check(source.isConnected && source.textContent === text && state.memory.activeMessageId === selected, 'resize must preserve selected original text');
  check(compose.querySelector('[data-memory-field=title]') === title && compose.querySelector('[data-memory-field=content]') === content && state.memory.draft === draft, 'resize must preserve editing nodes and draft');
  check(title.value === titleValue && content.value === contentValue, 'resize must preserve unsaved input');
  if (columns < 3) {
    const editor = compose.querySelector('.memory-editor');
    check(getComputedStyle(editor).flexDirection === 'column', 'narrow editor must stack');
    const save = compose.querySelector('[data-action=save-memory]');
    save.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const rect = save.getBoundingClientRect();
    check(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)?.closest('button') === save, 'save action must be reachable');
  }
};

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false, width: 1800, height: 1000,
    webPreferences: { preload: path.join(__dirname, '../fixtures/context-ui-test-preload.cjs'), contextIsolation: false, sandbox: false },
  });
  await win.loadFile(path.join(__dirname, '../../src/renderer/index.html'));
  await win.webContents.executeJavaScript(`(async () => {
    for (let i = 0; i < 100 && !state.tasks.length; i++) await new Promise(resolve => setTimeout(resolve, 20));
    state.inspector.open = false;
    state.view = 'memory'; openMemoryCompose(); await pickMemorySession('root');
    document.querySelector('.memory-msg').click();
    document.querySelector('[data-action=add-checked-messages]').click();
    for (const [field, value] of [['title', '整理中的标题'], ['content', '整理中的记忆草稿']]) {
      const input = document.querySelector('[data-memory-field=' + field + ']');
      input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  })()`);
  for (const width of [1200, 960, 900, 720, 600, 560, 360, 960, 1200]) {
    await win.webContents.executeJavaScript(`(${checkMemoryLayout})(${width})`);
  }
  console.log('memory responsive checks passed (9 width transitions)');
  win.destroy(); app.quit();
}).catch(error => { console.error(error); app.exit(1); });
