/*
 * 本文件以真实 Electron 和确定性本地 API 验证 Context 策展 Patrol 桌面交互。
 * 输入为普通/策展草稿、跟踪状态与分页 Revision，输出为表单隔离、Avatar、详情、
 * 生命周期操作、审计记录和受管 Context 导航断言。
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const userData = path.join(os.tmpdir(), `focus-context-curator-ui-${process.pid}`);
fs.mkdirSync(userData, { recursive: true });
app.setPath("userData", userData);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

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
  await win.loadFile(path.join(__dirname, "index.html"));
  await win.webContents.executeJavaScript(`new Promise(async (resolve, reject) => {
    for (let count = 0; count < 150; count += 1) {
      if (typeof openDraft === 'function' && state.activeTaskId) return resolve();
      await new Promise(next => setTimeout(next, 20));
    }
    reject(new Error('桌面初始化超时'));
  })`);

  const draft = await win.webContents.executeJavaScript(`(async () => {
    await openDraft('empty');
    document.querySelector('[data-action="set-patrol-mode"][data-mode="context_curator"]').click();
    for (let count = 0; count < 100 && state.drafts.get('empty')?.mode !== 'context_curator'; count += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const instructions = document.querySelector('[data-curation-policy="instructions"]');
    instructions.value = '保留目标、约束和最终成功结果';
    instructions.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 450));
    const saved = window.__patrolAvatarTest.draftSaves.at(-1)?.body;
    return {
      mode: state.drafts.get('empty')?.mode,
      hasPolicy: Boolean(instructions),
      hasHistory: Boolean(document.querySelector('#historyList')),
      hasFinalMessage: Boolean(document.querySelector('#draftFinalMessage')),
      boundary: document.querySelector('.draft-panel').textContent.includes('无通用工具'),
      saved,
    };
  })()`);
  if (draft.mode !== "context_curator" || !draft.hasPolicy || draft.hasHistory || draft.hasFinalMessage || !draft.boundary) {
    throw new Error(`策展草稿表单隔离失败: ${JSON.stringify(draft)}`);
  }
  if (draft.saved?.mode !== "context_curator" || draft.saved?.curation_policy?.instructions !== "保留目标、约束和最终成功结果") {
    throw new Error(`策展草稿保存契约失败: ${JSON.stringify(draft.saved)}`);
  }
  if (draft.saved?.equipment?.permissions?.join(",") !== "read" || draft.saved?.equipment?.skills?.length) {
    throw new Error(`策展权限边界失败: ${JSON.stringify(draft.saved?.equipment)}`);
  }

  const details = await win.webContents.executeJavaScript(`(async () => {
    const originalFetch = window.fetch;
    window.__curationState = {
      agent_id: 'curator-agent-0001',
      control_state: 'paused',
      health_state: 'degraded',
      root_context: { context_id: 'root', title: '其他会话' },
      managed_context: { context_id: 'child', title: 'Patrol 会话' },
      desired_checkpoint_id: 'checkpoint-target-2222',
      observed_checkpoint_id: 'checkpoint-target-2222',
      prepared_checkpoint_id: 'checkpoint-published-1111',
      published_checkpoint_id: 'checkpoint-published-1111',
      binding_revision: 3,
      last_error: '上一次结构化结果无效',
      latest_run: { run_id: 'curator-run', status: 'success', error: null },
      latest_revision: { revision_id: 'rev-2', source_checkpoint_id: 'checkpoint-target-2222', status: 'error', attempts: [{ attempt_number: 1, model_name: 'deepseek-v4-flash', output_method: 'prompt_json', status: 'error', error_kind: 'parse', error: '结构化结果无效' }] },
      revisions: [{
        revision_id: 'rev-2', source_checkpoint_id: 'checkpoint-target-2222', status: 'error',
        projection_status: null, published_at: null, error: '结构化结果无效',
        attempts: [{ attempt_number: 1, model_name: 'deepseek-v4-flash', output_method: 'prompt_json', status: 'error', error_kind: 'parse', error: '结构化结果无效' }],
        source_payload: { source_snapshot: { messages: [{ source_message_id: 'm-error', role: 'tool', content: 'timeout' }] } },
        disposition_manifest: [{ source_message_id: 'm-error', action: 'discarded', reason_category: 'failed_tool_noise', target_message_indexes: [], plan_items: [] }],
      }],
      next_cursor: 'rev-2',
    };
    window.fetch = async (input, options = {}) => {
      const url = new URL(String(input), 'http://focus.test');
      if (url.pathname === '/desktop/api/agents/curator-agent-0001/context-curation' && options.method !== 'PUT') {
        if (url.searchParams.get('before')) return new Response(JSON.stringify({
          ...window.__curationState,
          revisions: [{
            revision_id: 'rev-1', source_checkpoint_id: 'checkpoint-published-1111', status: 'published',
            projection_status: 'valid', published_at: '2026-09-03T01:00:00Z', error: null,
            attempts: [{ attempt_number: 1, model_name: 'deepseek-v4-flash', output_method: 'prompt_json', status: 'success', error: null, parsed_response: { items: [{ type: 'compose_message' }] } }],
            source_payload: { source_snapshot: { messages: [{ source_message_id: 'm-goal', role: 'human', content: '交付目标' }] } },
            disposition_manifest: [{ source_message_id: 'm-goal', action: 'used', reason_category: 'selected_by_plan', target_message_indexes: [0], plan_items: [{ plan_item_type: 'compose_message' }] }],
          }],
          next_cursor: null,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        return new Response(JSON.stringify(window.__curationState), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.pathname === '/desktop/api/agents/curator-agent-0001/context-curation/state' && options.method === 'PUT') {
        const body = JSON.parse(options.body);
        window.__curationState.control_state = body.state;
        window.__curationState.health_state = 'idle';
        window.__patrolAvatarTest.agents.empty[0].control_state = body.state;
        window.__patrolAvatarTest.agents.empty[0].health_state = 'idle';
        return new Response(JSON.stringify(window.__curationState), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return originalFetch(input, options);
    };
    window.__patrolAvatarTest.agents.empty.push({
      agent_id: 'curator-agent-0001', checkpoint_ns: 'patrol:curator-agent-0001', mode: 'context_curator',
      control_state: 'paused', health_state: 'degraded', permissions: ['read'], root_context: window.__curationState.root_context,
      managed_context: window.__curationState.managed_context,
      desired_checkpoint_id: window.__curationState.desired_checkpoint_id,
      observed_checkpoint_id: window.__curationState.observed_checkpoint_id,
      prepared_checkpoint_id: window.__curationState.prepared_checkpoint_id,
      published_checkpoint_id: window.__curationState.published_checkpoint_id,
      latest_run: window.__curationState.latest_run,
    });
    const tree = state.contextTrees.get('workspace');
    tree.find(item => item.context_id === 'child').managed_status = 'paused';
    state.activeTaskId = 'empty';
    state.view = 'focus';
    await hydrateActive('empty');
    render();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const avatar = document.querySelector('[data-agent-id="curator-agent-0001"]');
    avatar.querySelector('.patrol-avatar__button').click();
    const avatarStatus = avatar.querySelector('.patrol-avatar__status').textContent;
    await openAgentDetails('curator-agent-0001');
    const panel = document.querySelector('.curation-detail');
    const initialRevisionCount = panel.querySelectorAll('.curation-revision').length;
    panel.querySelector('[data-action="load-more-curation"]').click();
    for (let count = 0; count < 100 && document.querySelectorAll('.curation-revision').length !== 2; count += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const auditText = document.querySelector('.curation-audit').textContent;
    const pagedRevisionCount = document.querySelectorAll('.curation-revision').length;
    document.querySelector('[data-action="set-curation-state"][data-state="following"]').click();
    for (let count = 0; count < 100 && state.agentDetails.curation?.control_state !== 'following'; count += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    return {
      avatarStatus,
      panelText: panel.textContent,
      initialRevisionCount,
      pagedRevisionCount,
      auditText,
      tracking: state.agentDetails.curation?.control_state,
      hasManagedLink: Boolean(document.querySelector('[data-action="open-managed-context"][data-context-id="child"]')),
      hasConversationHistory: Boolean(document.querySelector('.agent-inspector-detail .agent-detail-history')),
      railText: renderContextRail(activeTask()),
    };
  })()`);
  if (details.avatarStatus !== "跟踪已暂停" || !details.panelText.includes("等待首次发布") && !details.panelText.includes("Patrol 会话")) {
    throw new Error(`策展 Avatar 或详情状态失败: ${JSON.stringify(details)}`);
  }
  if (details.initialRevisionCount !== 1 || details.pagedRevisionCount !== 2 || !details.auditText.includes("failed_tool_noise") || !details.auditText.includes("selected_by_plan") || !details.auditText.includes("compose_message") || !details.auditText.includes("来源证据")) {
    throw new Error(`策展审计分页失败: ${JSON.stringify(details)}`);
  }
  if (details.tracking !== "following" || !details.hasManagedLink || details.hasConversationHistory || !details.railText.includes("受管 已暂停")) {
    throw new Error(`策展操作或 Context rail 失败: ${JSON.stringify(details)}`);
  }

  const navigated = await win.webContents.executeJavaScript(`(async () => {
    document.querySelector('[data-action="open-managed-context"]').click();
    for (let count = 0; count < 100 && state.activeTaskId !== 'child'; count += 1) await new Promise(resolve => setTimeout(resolve, 10));
    return { taskId: state.activeTaskId, view: state.view };
  })()`);
  if (navigated.taskId !== "child" || navigated.view !== "focus") {
    throw new Error(`受管 Context 导航失败: ${JSON.stringify(navigated)}`);
  }

  console.log("context-curator-ui-e2e: 草稿、Avatar、详情、状态操作、审计分页与 Context 导航通过");
  win.destroy();
}

app.whenReady().then(run).then(() => app.quit()).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});
