/*
 * 本文件对外提供 F20 桌面视觉基线截图生成器。输入为确定性 preload、阶段目录和 focus/map/
 * draft/plugins/materials/file/Commitment/压缩/Context 目标，输出为三种验收尺寸 PNG；工作流使用
 * 真实 index.html、插件脚本和 Electron 渲染器，不访问网络或数据库。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const testUserData = path.join(os.tmpdir(), `focus-f20-qa-${process.pid}`);
fs.mkdirSync(testUserData, { recursive: true });
app.setPath("userData", testUserData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("no-sandbox");

const windows = [];

const stage = process.argv[2] || "before";
const target = process.argv[3] || "focus";
const changeId = process.argv[4] || "f20-frontend-ui-refresh";
const sizes = [
  { name: "1440x1024", width: 1440, height: 1024 },
  { name: "1200x800", width: 1200, height: 800 },
  { name: "900x680", width: 900, height: 680 },
];

async function capture(size, outputDir) {
  const win = new BrowserWindow({
    show: false,
    width: size.width,
    height: size.height,
    webPreferences: {
      preload: path.join(__dirname, "context-ui-test-preload.cjs"),
      contextIsolation: false,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  windows.push(win);
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 150; count += 1) {
      if (document.querySelector('.focus-view')) {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
        return;
      }
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('等待 Focus 视图超时'));
  })`);
  if (target === "file" || target === "file-docx") {
    await win.webContents.insertCSS(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "style.css"), "utf8"));
    await win.webContents.executeJavaScript(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "viewer.js"), "utf8"));
    await win.webContents.executeJavaScript(fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "entry.js"), "utf8"));
  }
  await win.webContents.executeJavaScript(`(async () => {
    const target = ${JSON.stringify(target)};
    if (target === 'map') {
      document.querySelector('[data-action="show-map"]').click();
    } else if (target === 'draft') {
      const task = state.tasks.find(item => item.task_id === 'child') || state.tasks[0];
      state.activeTaskId = task.task_id;
      state.equipment = { models: [{ name: 'local-model', display_name: 'Local Model', context_window: 32000 }], tools: [], skills: [], permissions: ['read', 'write', 'host_command'] };
      state.drafts.set(task.task_id, {
        task_id: task.task_id, draft_id: 'qa-draft', source_checkpoint_id: 'checkpoint-qa-full-identifier',
        system_prompt: 'You are a focused local worker. Preserve the workspace constraints.',
        history_messages: [
          { role: 'human', content: '先理解当前工作区结构。' },
          { role: 'ai', content: '已读取结构，等待具体实现目标。' },
          { role: 'tool', name: 'read_file', tool_call_id: 'qa-call', content: 'package and source tree loaded', locked: true }
        ],
        final_human_message: '完成指定模块并运行相关测试，最后给出简洁结果。', token_estimate: 1840,
        equipment: { model_name: 'local-model', skills: [], permissions: ['read', 'write'] }
      });
      state.view = 'draft';
      render();
    } else if (target === 'plugins') {
      state.inspector.open = false;
      state.plugins = {
        filter: 'all', selectedName: 'spatial-patrol',
        plugins: [
          { name: 'demo', version: '1.0.0', status: 'active', injected: ['tool', 'hook.before_model'], requires: [] },
          { name: 'dsh-eyes', version: '0.1.0', status: 'active', injected: ['tool', 'service.vision'], requires: [] },
          { name: 'spatial-patrol', version: '0.1.0', status: 'active', injected: ['service.spatial'], requires: ['dsh-eyes'] },
          { name: 'broken-extension', version: '0.2.0', status: 'rejected', injected: [], requires: [], conflict: { interface: 'tool', current: 'demo', new: 'broken-extension' } }
        ],
        interfaces: {
          tool: { plugins: ['demo', 'dsh-eyes'], cardinality: 'multiple' },
          'hook.before_model': { plugins: ['demo'], cardinality: 'multiple' },
          'service.vision': { plugins: ['dsh-eyes'], cardinality: 'single', read_only: true },
          'service.spatial': { plugins: ['spatial-patrol'], cardinality: 'single', read_only: true }
        },
        traces: [
          { interface: 'service.spatial', plugin: 'spatial-patrol', status: 'success', duration_ms: 12 },
          { interface: 'service.spatial', plugin: 'spatial-patrol', status: 'failed', duration_ms: 4, error: '示例：文件暂时被占用' }
        ]
      };
      state.view = 'plugins';
      render();
    } else if (target === 'materials') {
      state.materials.set(state.activeTaskId, [
        { material_id: 'm1', relative_path: 'specs/product-direction.md', reading_mode: 'full', instruction_mode: 'strict', retention: 'irreplaceable', needs_confirmation: true },
        { material_id: 'm2', relative_path: 'research/competitive-notes.txt', reading_mode: 'rough', instruction_mode: 'reference', retention: 'removable', needs_confirmation: false }
      ]);
      state.inspector.open = true;
      state.inspector.tab = 'materials';
      render();
    } else if (target === 'file' || target === 'file-docx') {
      const fileName = target === 'file-docx' ? 'reports/research-draft.docx' : 'specs/product-direction.md';
      const material = { material_id: 'm-file', relative_path: fileName, reading_mode: 'full', instruction_mode: 'strict', retention: 'irreplaceable', needs_confirmation: false };
      state.materials.set(state.activeTaskId, [material]);
      state.filesPanel = material;
      state.panelWidth = Math.min(520, Math.max(380, Math.round(window.innerWidth * 0.42)));
      state.inspector.open = false;
      state.view = 'focus';
      render();
    } else if (target === 'agents') {
      const task = state.tasks.find(item => item.task_id === 'child') || state.tasks[0];
      state.activeTaskId = task.task_id;
      state.agents.set(task.task_id, [
        { agent_id: 'agent-running', permissions: ['read'], checkpoint_ns: 'audit/worker', latest_run: { status: 'running' } },
        { agent_id: 'agent-complete', permissions: ['read', 'write'], checkpoint_ns: 'audit/reviewer', latest_run: { status: 'success' } }
      ]);
      state.agentDialog = { agentId: null, messages: [], busy: false };
      state.view = 'focus';
      state.inspector.open = true;
      state.inspector.tab = 'agents';
      render();
    } else if (target === 'commitment') {
      state.inspector.open = false;
      state.commitment.taskId = state.activeTaskId;
      state.commitment.stage = 4;
      render();
      appendCommitmentTrace({ actor: 'supervisor', stage: 4, attempt: 1, title: 'Supervisor 委派范围审查', status: 'completed', detail: '已将验收标准与文件范围交给 Worker。', payload: { reasoning_summary: '先确认修改边界，再进入实现。' } });
      appendCommitmentTrace({ actor: 'worker', stage: 4, attempt: 1, title: 'Worker 返回实施方案', status: 'completed', detail: '建议保留同源拓扑并仅更新展示层。', payload: { files: ['desktop/app.js', 'desktop/styles/views.css'] } });
      showReview({ stage: 4, draft: { summary: '保留同源 API/SSE 与插件资源，继续完成界面升级。', acceptance: ['无新增依赖', '不改变后端状态枚举', '900×680 无页面横向滚动'] }, allowed_decisions: ['approve', 'revise'] });
    } else if (target === 'compression') {
      state.inspector.open = false;
      state.compression = {
        taskId: state.activeTaskId,
        request: { usage: 28640, limit: 32000, ratio: 0.9 },
        messages: [
          { id: 'c1', role: 'human', content: '请先审阅当前前端结构与约束。' },
          { id: 'c2', role: 'ai', content: '已确认项目使用原生 JavaScript、HTML 与 CSS。' },
          { id: 'c3', role: 'tool', name: 'read_file', tool_call_id: 'call-1', content: 'desktop/app.js and styles loaded' },
          { id: 'c4', role: 'human', content: '保持前后端同源，不要引入跨域。' },
          { id: 'c5', role: 'ai', content: '同源约束已记录，并加入静态守卫。' }
        ],
        selected: new Set([3, 4]),
        ranges: [{ source_ids: ['c1', 'c2', 'c3'], replacement: '已审阅原生前端结构，确认零新增依赖并保留现有 API。', generated: true, restore: false, summarizing: false }],
        busy: false, recovery: null
      };
      state.view = 'compress';
      render();
    } else if (target === 'context') {
      state.inspector.open = false;
      await reopenContextDecision('blocked');
    } else if (target === 'empty') {
      state.tasks = [];
      state.inspector.open = false;
      state.view = 'focus';
      render();
    } else if (target === 'error') {
      const task = state.tasks.find(item => item.task_id === 'child') || state.tasks[0];
      state.activeTaskId = task.task_id;
      const detail = state.details.get(task.task_id);
      detail.messages = [
        { role: 'human', content: '修改工作区外的 README。' },
        { role: 'ai', content: '', tool_calls: [{ id: 'qa-error', name: 'edit_file', args: { path: 'C:/outside/README.md' } }] },
        { role: 'tool', tool_call_id: 'qa-error', name: 'edit_file', status: 'error', content: '路径不属于当前工作区: C:/outside/README.md' },
        { role: 'ai', content: '操作未执行。请选择当前工作区内的文件后重试。' }
      ];
      state.inspector.open = false;
      state.view = 'focus';
      render();
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  })()`);
  win.showInactive();
  await new Promise(resolve => setTimeout(resolve, 80));
  const image = await win.webContents.capturePage();
  fs.writeFileSync(path.join(outputDir, `focus-${size.name}.png`), image.toPNG());
  win.hide();
}

app.whenReady().then(async () => {
  const outputDir = path.join(__dirname, "..", "openspec", "changes", changeId, "qa", stage, target);
  fs.mkdirSync(outputDir, { recursive: true });
  for (const size of sizes) await capture(size, outputDir);
  for (const win of windows) win.destroy();
  console.log(`f20-qa-capture: ${stage}/${target} ${sizes.length} 张截图已生成`);
}).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
