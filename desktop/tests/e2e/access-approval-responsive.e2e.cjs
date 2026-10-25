/* 加载生产会话审批卡片，验证中英文在不同容器宽度下的操作可见性。
 * 使用离线 API fixture；只检查布局、焦点与命中区域，不提交审批决定。
 * 示例：electron desktop/tests/e2e/access-approval-responsive.e2e.cjs。
 */
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { app, BrowserWindow } = require('electron');

app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'focus-approval-layout-')));
app.disableHardwareAcceleration();

const checkApprovalLayout = async ({ width, locale }) => {
  FocusI18n.setLocale(locale);
  showAccessReview(activeTask(), {
    type: 'access_review', tool: 'write_file', cwd: 'C:/workspace',
    writes: ['C:/workspace/notes.txt'], access_mode: 'workspace-write',
  }, 'main:' + state.activeTaskId);
  const panel = document.querySelector('.access-review-panel');
  panel.style.width = width + 'px';
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const buttons = [...panel.querySelectorAll('[data-access-action]')].filter(button => !button.hidden);
  const check = (value, message) => { if (!value) throw new Error(width + 'px/' + locale + ': ' + message); };
  check(buttons.length === 4, 'expected approval actions must be present');
  let previousBottom = -Infinity;
  for (const button of buttons) {
    button.focus();
    button.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const rect = button.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    check(rect.left >= panelRect.left && rect.right <= panelRect.right, 'button is clipped: ' + button.textContent);
    check(button.scrollWidth <= button.clientWidth + 1, 'button text overflows: ' + button.textContent);
    check(document.activeElement === button, 'button must be keyboard reachable');
    check(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)?.closest('button') === button, 'button must be clickable');
    if (panel.clientWidth <= 420) {
      check(button.offsetTop >= previousBottom, 'narrow card must place actions on separate rows');
      previousBottom = button.offsetTop + rect.height;
    }
  }
  closeAccessReview();
};

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false, width: 1680, height: 1000,
    webPreferences: {
      preload: path.join(__dirname, '../fixtures/context-ui-test-preload.cjs'),
      contextIsolation: false, sandbox: false,
    },
  });
  await win.loadFile(path.join(__dirname, '../../src/renderer/index.html'));
  await win.webContents.executeJavaScript(`(async () => {
    for (let i = 0; i < 100 && !state.tasks.length; i++) await new Promise(resolve => setTimeout(resolve, 20));
    await goFocusHome();
  })()`);
  for (const width of [240, 320, 420, 421, 600]) {
    for (const locale of ['zh-CN', 'en-US']) {
      await win.webContents.executeJavaScript(`(${checkApprovalLayout})(${JSON.stringify({ width, locale })})`);
    }
  }
  console.log('approval responsive checks passed (10 cases)');
  win.destroy();
  app.quit();
}).catch(error => { console.error(error); app.exit(1); });
