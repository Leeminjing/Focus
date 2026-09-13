/*
 * 本文件以真实 Electron 验证会话页四列布局与文件预览列。输入为确定性测试 preload、四种窗口宽度与
 * 五种预览策略的材料记录，输出为「未点文件不占位、点开即出现、四栏不溢出、分隔条与预览列左缘一致、
 * 窄屏转覆盖层、五种策略均无空白」断言。
 * 运行：`npx electron file-preview-layout.e2e.cjs`（需要完整桌面栈）。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-file-preview-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const cases = [
  { width: 1600, height: 1000, zoom: 1 },
  { width: 1280, height: 840, zoom: 1 },
  { width: 1280, height: 840, zoom: 1.25 },
  { width: 1000, height: 720, zoom: 1 },
];

// 五种渲染策略各一份材料；binary 用无对应材料记录的文件名触发信息卡。
const materials = [
  { material_id: "f-image", relative_path: "shots/cover.png", size_bytes: 4096, is_image: true },
  { material_id: "f-doc", relative_path: "specs/product-direction.md", size_bytes: 2048 },
  { material_id: "f-text", relative_path: "logs/run.log", size_bytes: 512 },
  { material_id: "f-pdf", relative_path: "reports/summary.pdf", size_bytes: 8192 },
  { material_id: "f-bin", relative_path: "reports/research-draft.docx", size_bytes: 9999 },
];

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function run() {
  const failures = [];
  const win = new BrowserWindow({
    show: false,
    width: 1600,
    height: 1000,
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
    reject(new Error('Focus 启动超时'));
  })`);
  win.showInactive();

  for (const item of cases) {
    win.setSize(item.width, item.height);
    win.webContents.setZoomFactor(item.zoom);
    await wait(160);
    const result = await win.webContents.executeJavaScript(`(() => {
      const materials = ${JSON.stringify(materials)};
      state.materials.set(state.activeTaskId, materials);
      state.view = 'focus';
      resetFilePreviews();
      render();

      const shell = document.querySelector('.app-shell');
      const previewNode = document.querySelector('.file-preview');
      const resizer = document.querySelector('.shell-resizer-preview');
      const before = {
        hidden: previewNode.hidden,
        width: previewNode.getBoundingClientRect().width,
        resizerHidden: resizer.getAttribute('aria-hidden'),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };

      const kinds = {};
      for (const material of materials) {
        openFilePreview(material);
        const node = document.querySelector('.file-preview');
        const body = document.querySelector('#filePreviewBody');
        kinds[material.material_id] = {
          hidden: node.hidden,
          width: Math.round(node.getBoundingClientRect().width),
          tabCount: document.querySelectorAll('.file-preview-tab').length,
          mounted: body.children.length,
          empty: body.textContent.trim() === '' && body.children.length === 0,
          role: getComputedStyle(node).position,
        };
      }

      // 分隔条应压在预览列左缘上（±6px 容差覆盖手柄宽度与取整）
      const previewRect = previewNode.getBoundingClientRect();
      const resizerRect = resizer.getBoundingClientRect();
      const workspaceRect = document.querySelector('.app-workspace').getBoundingClientRect();
      const inspectorRect = document.querySelector('#appInspector').getBoundingClientRect();
      const order = [workspaceRect, previewRect, inspectorRect].map(rect => Math.round(rect.left));

      return {
        before,
        kinds,
        after: {
          hidden: previewNode.hidden,
          width: Math.round(previewRect.width),
          resizerVisible: resizer.getAttribute('aria-hidden') === 'false',
          resizerDelta: Math.abs((resizerRect.left + resizerRect.width / 2) - previewRect.left),
          workspaceWidth: Math.round(workspaceRect.width),
          orderAscending: order[0] <= order[1] && order[1] <= order[2],
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          tabs: document.querySelectorAll('.file-preview-tab').length,
        },
        viewport: innerWidth,
      };
    })()`);

    const label = `${item.width}x${item.height}@${item.zoom}`;
    if (!result.before.hidden || result.before.width > 0) failures.push(`${label} 未点文件时预览列占位`);
    if (result.before.resizerHidden !== "true") failures.push(`${label} 未打开文件时分隔条仍可见`);
    if (result.after.hidden || result.after.width <= 0) failures.push(`${label} 点开后预览列未出现`);
    if (!result.after.orderAscending) failures.push(`${label} 四列顺序不是 会话区 → 预览列 → 检查器`);
    if (result.after.overflow > 1 || result.before.overflow > 1) failures.push(`${label} 出现横向溢出`);
    if (result.after.tabs !== materials.length) failures.push(`${label} 标签页数量为 ${result.after.tabs}`);
    if (result.after.workspaceWidth < 320) failures.push(`${label} 会话区被压缩到 ${result.after.workspaceWidth}`);

    // 宽屏下分隔条与预览列左缘对齐；窄屏转覆盖层时手柄隐藏、不参与定位
    if (result.viewport > 1180) {
      if (!result.after.resizerVisible) failures.push(`${label} 宽屏下预览分隔条不可见`);
      if (result.after.resizerDelta > 6) failures.push(`${label} 分隔条偏离预览列左缘 ${result.after.resizerDelta}px`);
    } else if (result.kinds["f-text"].role !== "absolute") {
      failures.push(`${label} 窄屏下预览列未转为覆盖层（${result.kinds["f-text"].role}）`);
    }

    for (const [id, kind] of Object.entries(result.kinds)) {
      if (kind.hidden) failures.push(`${label} ${id} 打开后预览列被隐藏`);
      if (kind.empty) failures.push(`${label} ${id} 出现空白预览区`);
      if (kind.mounted < 1) failures.push(`${label} ${id} 未挂载任何预览节点`);
    }

    // 未登记为材料、且位于工作区之外的绝对路径同样必须可预览
    const outside = await win.webContents.executeJavaScript(`(async () => {
      resetFilePreviews();
      render();
      openFilePreview({ path: "C:/outside/workspace/notes/outside.md", relative_path: "notes/outside.md" });
      await new Promise(resolve => setTimeout(resolve, 150));
      const body = document.querySelector("#filePreviewBody");
      return {
        hidden: document.querySelector(".file-preview").hidden,
        head: body.querySelector(".file-preview-head")?.textContent || "",
        text: body.querySelector(".file-preview-text")?.textContent || "",
        tabs: [...document.querySelectorAll(".file-preview-tab-label")].map(node => node.textContent),
      };
    })()`);
    if (outside.hidden) failures.push(`${label} 工作区之外的绝对路径未能打开预览`);
    if (!outside.head.includes("outside.md")) failures.push(`${label} 未呈现工作区之外文件的名称：${outside.head}`);
    if (!outside.text.includes("按路径预览的正文")) failures.push(`${label} 未呈现按路径读取的正文：${outside.text}`);
    if (!outside.tabs.some(name => name.includes("outside.md"))) failures.push(`${label} 未生成该文件的标签页`);

    // 未登记为材料的文件卡片必须是可点按钮，而不是不可点占位
    const card = await win.webContents.executeJavaScript(`(() => {
      const task = state.tasks.find(item => item.task_id === state.activeTaskId) || state.tasks[0];
      state.materials.set(task.task_id, []);
      const markup = renderFileCard({ filename: "notes/unregistered.md", size: 12 }, task);
      return { markup, isButton: markup.startsWith("<button"), plain: markup.includes("is-plain") };
    })()`);
    if (!card.isButton || card.plain) failures.push(`${label} 未登记文件卡片仍不是可点按钮：${card.markup.slice(0, 80)}`);

    // 回归：点击 file:// 正文链接必须真的读到文件。曾因入口只取 basename、丢掉绝对路径，
    // 而让按路径读取报「仅接受绝对路径」并退化成一张没有内容的卡片。
    const viaLink = await win.webContents.executeJavaScript(`(async () => {
      resetFilePreviews();
      render();
      const task = state.tasks.find(item => item.task_id === state.activeTaskId) || state.tasks[0];
      state.materials.set(task.task_id, []);
      const anchor = document.createElement("a");
      anchor.setAttribute("href", "file:///C:/workspace/%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md");
      anchor.textContent = "文件列表.md";
      document.querySelector("#app").appendChild(anchor);
      anchor.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      await new Promise(resolve => setTimeout(resolve, 180));
      const body = document.querySelector("#filePreviewBody");
      return {
        path: state.filePreview.shelf.active()?.path || "",
        text: body.querySelector(".file-preview-text")?.textContent || "",
        note: body.querySelector(".file-preview-empty")?.textContent || "",
      };
    })()`);
    if (!/^[A-Za-z]:/.test(viaLink.path)) {
      failures.push(`${label} file:// 链接未交出绝对路径：${JSON.stringify(viaLink)}`);
    }
    if (viaLink.note) failures.push(`${label} file:// 链接仍降级并报错：${viaLink.note}`);
    if (!viaLink.text) failures.push(`${label} file:// 链接未呈现正文：${JSON.stringify(viaLink)}`);
  }

  win.destroy();
  if (failures.length) {
    console.error("file-preview-layout 失败：");
    for (const line of failures) console.error(` - ${line}`);
    process.exitCode = 1;
    return;
  }
  console.log("file-preview-layout: 四列布局与五种预览策略断言通过");
}

app.whenReady().then(run).catch(error => {
  console.error("file-preview-layout 运行失败：", error);
  process.exitCode = 1;
});
