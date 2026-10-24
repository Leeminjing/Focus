/*
 * 本文件对外提供记忆库的纯渲染器。输入为记忆库列表与整理态状态，
 * 输出为可检索记忆卡片/列表、区分加载失败/空库/零匹配及三栏整理器 HTML；工作流在完整已加载列表检索，保留创建/编辑/来源/合并/分段，不改变持久化语义。示例：FocusMemoryView.render(memories, { query: "本地" })。
 * 策略分类展示三段正文并提供范围/启停编辑，普通记忆继续使用原来源整理器。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMemoryView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  // 布局比例（上下板 / 左右编辑区）统一钳制到合法区间，避免渲染出 <0 或 >1 的 flex。
  function clampRatio(value, min, max) {
    const n = Number(value);
    if (!Number.isFinite(n)) return min;
    return Math.min(max, Math.max(min, n));
  }

  // 分段记忆目标区：每段一个可编辑卡（标题 + 摘要正文）。
  function renderSegments(segments) {
    const items = (segments || []).map((seg, index) => {
      const title = seg?.title || localize`来源 ${index + 1}`;
      const body = seg?.body || "";
      return localize`<article class="memory-segment" data-segment-index="${index}">
        <header class="memory-segment-head">
          <span class="memory-segment-label">${index + 1}</span>
          <input class="memory-segment-title" data-segment-field="title" data-segment-index="${index}" placeholder="段标题" value="${escapeHtml(title)}">
        </header>
        <textarea class="memory-segment-body" data-segment-field="body" data-segment-index="${index}" rows="4" placeholder="本段压缩摘要（可编辑）">${escapeHtml(body)}</textarea>
      </article>`;
    }).join("");
    return items || localize('<p class="memory-empty-edit muted">切换「分段记忆」后点「重新压缩」，每段来源会压缩成独立记忆段。</p>');
  }

  const KIND_LABELS = { get full_session() { return localize("完整会话"); }, get partial_messages() { return localize("部分消息"); }, get partial_text() { return localize("部分文字"); }, get manual() { return localize("手动输入"); }, get merged() { return localize("合并来源"); } };

  function roleLabel(role) {
    return { human: localize("用户"), user: localize("用户"), ai: localize("助手"), assistant: localize("助手"), tool: localize("工具"), system: localize("系统") }[role] || role || localize("消息");
  }

  function messageText(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.map(item => typeof item === "string" ? item : (item && item.text) || "").join("\n");
    return String(content || "");
  }

  function messagePreview(content) {
    const text = messageText(content).replace(/\s+/g, " ").trim();
    if (!text) return localize("（空）");
    return text.length > 140 ? text.slice(0, 140) + "…" : text;
  }

  function isDialogueRole(role) {
    return role === "human" || role === "user" || role === "ai" || role === "assistant";
  }

  // === 浏览态：左记忆列表，右单条详情 ===

  const strategyText = memory => memory.category === "evolution_strategy" ? Object.values(memory.strategy || {}).join("\n") : memory.content;

  function card(memory, selected) {
    const kind = memory.category === "evolution_strategy" ? localize`工作策略 · ${memory.enabled ? localize("启用") : localize("停用")}${memory.locally_modified ? localize(" · 本地已修改") : ""}` : KIND_LABELS[memory.source_kind] || memory.source_kind || localize("手动输入");
    return `<button type="button" class="memory-card${selected ? " is-selected" : ""}" data-action="select-memory" data-memory-id="${escapeHtml(memory.memory_id)}" aria-pressed="${selected}">
      <span class="memory-card-heading"><strong>${escapeHtml(memory.title)}</strong><span class="ui-badge is-neutral">${escapeHtml(kind)}</span></span>
      <span class="memory-card-content">${escapeHtml(strategyText(memory))}</span>
    </button>`;
  }

  function detail(memory) {
    const kind = memory.category === "evolution_strategy" ? localize`工作策略 · ${memory.enabled ? localize("启用") : localize("停用")}${memory.locally_modified ? localize(" · 本地已修改") : ""}` : KIND_LABELS[memory.source_kind] || memory.source_kind || localize("手动输入");
    return localize`<section class="memory-detail-card">
      <header class="memory-detail-heading">
        <div><span class="workspace-kicker">MEMORY</span><h2>${escapeHtml(memory.title)}</h2><p class="muted">${escapeHtml(kind)} · ${escapeHtml((memory.updated_at || "").slice(0, 10)) || ""}</p></div>
        <span class="ui-badge is-neutral">${escapeHtml(kind)}</span>
      </header>
      ${memory.category === "evolution_strategy" ? `<p class="muted">${escapeHtml(memory.strategy_package?.id || "")} · v${escapeHtml(memory.strategy_package?.version || "1.0.0")} · ${memory.workspace_id ? localize("工作区") : localize("全局")}</p>${Object.entries(memory.strategy || {}).map(([key, value]) => `<section><h3>${({applicability: localize("适用场景"), evolution: localize("演化过程"), inheritance: localize("历史继承")})[key] || escapeHtml(key)}</h3><pre class="memory-detail-content">${escapeHtml(value)}</pre></section>`).join("")}` : `<pre class="memory-detail-content">${escapeHtml(strategyText(memory))}</pre>`}
      <div class="memory-detail-actions">
        <button class="text-button" type="button" data-action="edit-memory" data-memory-id="${escapeHtml(memory.memory_id)}">编辑</button>
        ${memory.category !== "evolution_strategy" ? localize('<button type="button" class="text-button" data-action="extract-memory-strategy">提炼为策略</button>') : ""}
        ${memory.category === "evolution_strategy" ? localize`<button type="button" class="text-button" data-action="export-strategy" data-memory-id="${escapeHtml(memory.memory_id)}">导出 STRATEGY.md</button>` : ""}
        <button class="text-button danger" type="button" data-action="delete-memory" data-memory-id="${escapeHtml(memory.memory_id)}">删除</button>
      </div>
    </section>`;
  }

  function workbench(memories, options) {
    const strategies = options.category === "evolution_strategy";
    const query = String(options.query || "").trim().toLowerCase();
    const all = (memories || []).filter(memory => (!options.category || (memory.category || "memory") === options.category) && (!query || `${memory.title} ${strategyText(memory)}`.toLowerCase().includes(query)));
    const selected = all.find(m => m.memory_id === options.selectedId) || null;
    if (!all.length && strategies) return localize`<section class="memory-empty"><h2>没有匹配的工作策略</h2><p>编写或导入一份方法，让 Focus 按你的方法组织工作。</p><button class="primary" data-action="new-strategy">新建策略</button><button data-action="import-strategy">导入策略</button></section>`;
    if (!all.length) return localize`<section class="memory-empty"><span class="ui-icon icon-file-text" aria-hidden="true"></span><h2 data-i18n="${memories?.length ? "memory.no_matches" : "memory.empty_title"}">${memories?.length ? localize("没有匹配的记忆") : localize("还没有任何记忆")}</h2><p data-i18n="${memories?.length ? "memory.no_matches_help" : "memory.empty_help"}">${memories?.length ? localize("换一个关键词，或清空搜索查看全部记忆。") : localize("把重要的想法、经验和资料记录在这里，在需要时重新找到。")}</p><button class="primary" data-action="new-memory" data-i18n="memory.create">新建记忆</button></section>`;
    return localize`<div class="memory-workbench">
      <aside class="memory-list${options.layout === "list" ? " is-list" : ""}"><header><strong>${strategies ? localize("工作策略") : localize("记忆")}</strong><span class="memory-list-actions"><button type="button" class="text-button" data-action="${strategies ? "new-strategy" : "new-memory"}">新建</button><span>${all.length}</span></span></header>
        <div class="memory-list-scroll">${all.map(m => card(m, m === selected)).join("")}</div>
      </aside>
      ${selected ? `<main class="memory-detail">${detail(selected)}</main>` : ""}
    </div>`;
  }

  // === 整理态：三栏 + 记忆框 ===

  function compose(state) {
    // 第一栏：会话列表（按工作区分组，会话卡可拖拽 = 完整会话来源）
    const groups = new Map();
    for (const s of (state.sessions || [])) {
      const key = s.workspace_name || localize("本地工作区");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    }
    // 分组展开状态：显式展开的 + 选中会话所在分组（恒展开）。state 驱动，重建后恢复。
    const expanded = new Set(state.expandedGroups || []);
    const activeWorkspace = (state.sessions || []).find(s => s.task_id === state.activeSessionId)?.workspace_name || localize("本地工作区");
    if (state.activeSessionId) expanded.add(activeWorkspace);
    const sessions = [...groups.entries()].map(([workspace, items]) => `
      <details class="memory-session-group" data-group="${escapeHtml(workspace)}"${expanded.has(workspace) ? " open" : ""}>
        <summary class="memory-session-group-head" data-action="toggle-memory-group" data-group="${escapeHtml(workspace)}"><span class="memory-session-group-name">${escapeHtml(workspace)}</span><span class="memory-session-group-count">${items.length}</span></summary>
        <div class="memory-session-group-body">
          ${items.map(s => `
            <button type="button" class="memory-session-item${state.activeSessionId === s.task_id ? " is-active" : ""}" draggable="true" data-action="pick-session" data-context-id="${escapeHtml(s.task_id)}" data-context-title="${escapeHtml(s.title)}">
              <span class="memory-session-title">${escapeHtml(s.title)}</span>
              <span class="memory-session-meta">${s.thread_id ? escapeHtml(s.thread_id.slice(0, 8)) : ""}</span>
            </button>`).join("")}
        </div>
      </details>`).join("") || localize('<p class="muted">暂无可选会话。</p>');

    // 第二栏：对话消息（勾选多选不连续，消息卡可拖拽 = 选中的消息来源）
    const dialogue = (state.enabledMessages || []).filter(m => isDialogueRole(m.role) && messageText(m.content).trim());
    const selectedIds = new Set(state.selectedMessageIds || []);
    const list = dialogue.map((message, index) => {
      const id = message.id || `idx-${index}`;
      const checked = selectedIds.has(id);
      const active = state.activeMessageId === id;
      return `<button type="button" class="memory-msg${active ? " is-active" : ""}${checked ? " is-checked" : ""}" draggable="true" data-action="toggle-message" data-message-id="${escapeHtml(id)}" aria-pressed="${checked}">
        <div class="memory-msg-head"><strong>${String(index + 1).padStart(2, "0")} · ${escapeHtml(roleLabel(message.role))}</strong><span class="memory-msg-check">${checked ? "✓" : "+"}</span></div>
        <p class="memory-msg-preview">${escapeHtml(messagePreview(message.content))}</p>
      </button>`;
    }).join("");
    const listEmpty = localize('<p class="muted">点击左侧会话，选择要整理的对话。</p>');

    // 第三栏：当前消息全文（可划选文字高亮 = 部分文字来源，可拖拽）
    const sel = (state.enabledMessages || []).find(m => (m.id || "") === state.activeMessageId) || null;
    const source = sel
      ? localize`<article class="memory-source" data-message-id="${escapeHtml(sel.id)}">
          <div class="memory-source-head"><strong>${escapeHtml(roleLabel(sel.role))}</strong><button type="button" class="text-button" data-action="add-text">加入选中文字</button></div>
          <pre class="memory-source-text" data-selectable-text>${escapeHtml(messageText(sel.content))}</pre>
        </article>`
      : localize('<p class="muted">在左侧消息栏点选一条，查看完整原文并划选文字。</p>');

    // 记忆编辑区：左「记忆源·原始」(可编辑原文 + 拖拽重排 + 多选合并) + 右「压缩后的记忆」(可编辑)
    const sourceItems = (state.collectedSources || []).map((source, index) => {
      const kind = KIND_LABELS[source.kind] || source.kind;
      // 各 kind 组内序号，便于区分同 kind 多个来源。
      const sameKindIndex = (state.collectedSources || []).slice(0, index).filter(s => s.kind === source.kind).length + 1;
      const sameKindTotal = (state.collectedSources || []).filter(s => s.kind === source.kind).length;
      const kindOrdinal = sameKindTotal > 1 ? `（${sameKindIndex}/${sameKindTotal}）` : "";
      const label = source.kind === "session" ? `${escapeHtml(source.title || localize("会话"))}`
        : source.kind === "messages" ? localize`${source.count || 0} 条`
        : source.kind === "text" ? localize`划选文字${source.ranges?.length > 1 ? localize` · ${source.ranges.length} 段` : ""}`
        : source.kind === "merged" ? localize`${(source.subSources || []).length} 项合并`
        : escapeHtml(source.title || "");
      const isMergeSelected = (state.selectedSourcesForMerge || []).includes(index);
      // 拖拽重排仅在非合并模式下启用，避免与点选合并冲突。
      const dragAttr = state.mergeMode ? "" : localize` draggable="true" title="拖动可调整合并顺序"`;
      const cardClass = `memory-source-edit${state.mergeMode ? " is-mergeable" : ""}${isMergeSelected ? " is-merge-selected" : ""}`;
      const cardAction = state.mergeMode ? ` data-action="toggle-merge-source" data-source-index="${index}"` : "";
      const removeBtn = state.mergeMode ? "" : `<button type="button" class="text-button danger" data-action="remove-collected" data-collected-index="${index}">×</button>`;
      return localize`<article class="${cardClass}" data-source-index="${index}"${cardAction}>
        <header class="memory-source-edit-head"${dragAttr}>
          <strong>${index + 1}. ${escapeHtml(kind)}${kindOrdinal}</strong>
          <span class="memory-source-edit-label">${label}</span>
          ${state.mergeMode ? `<span class="memory-source-merge-check">${isMergeSelected ? "✓" : "+"}</span>` : removeBtn}
        </header>
        <textarea class="memory-source-edit-text" data-source-index="${index}" data-source-field="rawText" rows="4" placeholder="原始内容（可编辑）"${state.mergeMode ? " readonly" : ""}>${escapeHtml(source.rawText || source.text || "")}</textarea>
      </article>`;
    }).join("") || localize('<p class="memory-empty-edit muted">在上方选择会话 / 勾选消息 / 划选文字，添加记忆源。</p>');

    const editorRatio = clampRatio(state.editorRatio, 0.15, 0.85);
    const sourceRatio = clampRatio(state.sourceRatio, 0.2, 0.8);

    return localize`<div class="memory-compose">
      <div class="memory-compose-top" data-memory-top style="flex:${editorRatio} 1 0">
        <div class="memory-compose-grid">
          <aside class="memory-pane">
            <header><strong>对话</strong><span>${(state.sessions || []).length}</span></header>
            <div class="memory-session-list">${sessions}</div>
          </aside>
          <main class="memory-pane">
            <header><strong>消息</strong><span class="memory-pane-actions"><button type="button" class="text-button" data-action="add-checked-messages">加入勾选</button><span>${dialogue.length}</span></span></header>
            <div class="memory-msg-list">${list || listEmpty}</div>
          </main>
          <main class="memory-pane">
            <header><strong>原文</strong><span>${sel ? escapeHtml(roleLabel(sel.role)) : localize("未选")}</span></header>
            <div class="memory-source-wrap">${source}</div>
          </main>
        </div>
      </div>
      <div class="memory-resizer memory-resizer-v" data-memory-resizer-vertical role="separator" aria-orientation="horizontal"></div>
      <div class="memory-compose-bottom" data-memory-bottom style="flex:${1 - editorRatio} 1 0">
        <div class="memory-editor" data-memory-editor>
          <section class="memory-editor-sources" style="flex:${sourceRatio} 1 0">
            <header class="memory-editor-head">
              <strong>记忆源 · 原始完整视图</strong>
              <span class="memory-source-merge-actions">
                ${state.mergeMode
                  ? localize`<button type="button" class="text-button" data-action="confirm-merge-sources">合并所选</button>
                     <button type="button" class="text-button" data-action="cancel-merge-sources">取消</button>`
                  : localize`<button type="button" class="text-button" data-action="enter-merge-sources" ${(state.collectedSources || []).length < 2 ? "disabled" : ""}>合并</button>`}
              </span>
              <span>${(state.collectedSources || []).length}</span>
            </header>
            <div class="memory-source-edit-list${state.mergeMode ? " is-merge-mode" : ""}">${sourceItems}</div>
          </section>
          <div class="memory-resizer memory-resizer-h" data-memory-resizer-horizontal role="separator" aria-orientation="vertical"></div>
          <section class="memory-editor-target" style="flex:${1 - sourceRatio} 1 0">
            <header class="memory-editor-head">
              <strong>压缩后的记忆 · 完整视图</strong>
              <span class="memory-mode-toggle" role="group" aria-label="压缩语义">
                <button type="button" class="text-button${state.contentMode !== "segmented" ? " is-active" : ""}" data-action="set-memory-mode" data-mode="complete">完整记忆</button>
                <button type="button" class="text-button${state.contentMode === "segmented" ? " is-active" : ""}" data-action="set-memory-mode" data-mode="segmented">分段记忆</button>
              </span>
              <button type="button" class="text-button" data-action="recompress-memory">重新压缩</button>
            </header>
            <input class="memory-title-input" data-memory-field="title" placeholder="记忆标题" value="${escapeHtml(state.draftTitle || "")}">
            ${state.contentMode === "segmented" ? renderSegments(state.segments) : localize`<textarea class="memory-content-input" data-memory-field="content" placeholder="压缩总结后的完整记忆（可编辑）" rows="8">${escapeHtml(state.draftContent || "")}</textarea>`}
          </section>
        </div>
        <div class="memory-create-actions">
          <button class="primary" type="button" data-action="save-memory">保存记忆</button>
          <button type="button" data-action="generate-strategy-draft"${state.authoring?.busy ? " disabled" : ""}>${state.authoring?.busy ? localize("正在提炼…") : localize("从所选材料提炼策略")}</button>
          <button class="text-button" type="button" data-action="cancel-compose">取消</button>
        </div>
      </div>
    </div>`;
  }

  function strategyEditor(draft, workspaces, authoring = {}) {
    authoring = authoring || {};
    const fields = [["applicability", localize("适用场景")], ["evolution", localize("演化过程")], ["inheritance", localize("历史继承")]];
    return localize`<form class="memory-detail-card" data-strategy-form>
      <h2>${draft.memory_id ? localize("编辑") : localize("新建")}工作策略</h2>
      <p class="muted">规定何时采用、怎样推进，以及各项工作继承什么。保存后从后续轮次生效。</p>
      <fieldset class="strategy-editor-fields"${authoring.busy || authoring.saving ? " disabled" : ""}>
      <details><summary>从工作材料提炼策略</summary><label>经验、工作片段或希望 Focus 采用的方法<textarea rows="4" data-memory-field="strategy-source">${escapeHtml(draft.strategy_source || "")}</textarea></label><button type="button" data-action="generate-strategy-draft">生成可编辑草稿</button><p class="muted">生成后请检查适用范围和继承规则，再保存启用。</p></details>
      <label>标题<input data-memory-field="title" value="${escapeHtml(draft.title)}"></label>
      <label>策略 ID<input data-memory-field="package:id" placeholder="your-name/your-strategy（留空自动生成）" value="${escapeHtml(draft.strategy_package?.id || "")}"></label>
      <label>发布版本<input data-memory-field="package:version" placeholder="1.0.0" value="${escapeHtml(draft.strategy_package?.version || "1.0.0")}"></label>
      <label>简介<input data-memory-field="package:description" value="${escapeHtml(draft.strategy_package?.description || "")}"></label>
      ${fields.map(([key,label]) => `<label>${label}<textarea rows="4" data-memory-field="strategy:${key}">${escapeHtml(draft.strategy?.[key] || "")}</textarea></label>`).join("")}
      <label>生效范围<select data-memory-field="workspace_id"><option value="">全局</option>${workspaces.map(w => `<option value="${escapeHtml(w.id)}"${w.id === draft.workspace_id ? " selected" : ""}>${escapeHtml(w.name)}</option>`).join("")}</select></label>
      <label><input type="checkbox" data-memory-field="enabled"${draft.enabled !== false ? " checked" : ""}>启用</label>
      <button type="button" class="primary" data-action="save-strategy">${authoring.saving ? localize("正在保存…") : localize("保存策略")}</button>
      <details${draft.rehearsal_result ? " open" : ""}><summary>用三个场景预演方法</summary><p class="muted">调用当前模型生成推演，不执行工作或改变任务状态。</p>
        ${[["applicable", localize("预期应该采用")], ["inapplicable", localize("预期不必采用")], ["feedback", localize("收到新反馈后")]].map(([key, label]) => `<label>${label}<textarea rows="3" data-memory-field="scenario:${key}">${escapeHtml(draft.rehearsal_scenarios?.[key] || "")}</textarea></label>`).join("")}
        <button type="button" data-action="rehearse-strategy">预演策略</button>
        <div data-strategy-rehearsal-results>${draft.rehearsal_result ? Object.entries(draft.rehearsal_result).map(([key, result]) => localize`<section class="strategy-adoption"><h3>${({applicable: localize("应该采用场景"), inapplicable: localize("不必采用场景"), feedback: localize("反馈场景")})[key]}</h3><p><strong>${result.adopted ? localize("建议采用") : localize("建议不采用")}</strong>：${escapeHtml(result.reason)}</p><p>工作安排：${escapeHtml(result.organization)}</p><p>继承与排除：${escapeHtml(result.inheritance)}</p><p>缺少证据：${escapeHtml(result.missing_evidence.join("；") || localize("无额外证据要求"))}</p><p>停止条件：${escapeHtml(result.stop_when)}</p></section>`).join("") : ""}</div>
      </details></fieldset><p role="status">${authoring.busy ? localize("模型正在生成…") : escapeHtml(authoring.error || "")}</p>
      <button type="button" data-action="cancel-compose"${authoring.busy || authoring.saving ? " disabled" : ""}>取消</button></form>`;
  }

  function strategyImport(draft, workspaces) {
    const preview = draft.preview;
    const existing = preview?.existing;
    const labels = {applicability: localize("适用场景"), evolution: localize("演化过程"), inheritance: localize("历史继承")};
    return localize`<section class="memory-detail-card strategy-import"><h2>导入工作策略</h2><p class="muted">先预览内容，再选择范围并安装。文件中的指令仅作为策略材料。</p>
      <fieldset${draft.busy ? " disabled" : ""}><label>选择 STRATEGY.md<input type="file" accept=".md,.markdown,text/markdown,text/plain" data-strategy-file></label>
      <label>或粘贴文件内容<textarea rows="10" data-strategy-import-field="markdown" placeholder="---&#10;schema: focus.strategy/v1&#10;…">${escapeHtml(draft.markdown || "")}</textarea></label>
      <label>生效范围<select data-strategy-import-field="workspace_id"><option value="">全局</option>${workspaces.map(w => `<option value="${escapeHtml(w.id)}"${w.id === draft.workspace_id ? " selected" : ""}>${escapeHtml(w.name)}</option>`).join("")}</select></label>
      <label><input type="checkbox" data-strategy-import-field="enabled"${draft.enabled !== false ? " checked" : ""}>启用</label>
      <button type="button" data-action="preview-strategy">${draft.busy ? localize("处理中…") : localize("预览策略")}</button><button type="button" data-action="cancel-strategy-import">取消</button>
      <p role="alert" data-strategy-import-error>${escapeHtml(draft.error || "")}</p>
      ${preview ? localize`<div data-strategy-import-preview><h3>${escapeHtml(preview.metadata.name)} · v${escapeHtml(preview.metadata.version)}</h3><p>${escapeHtml(preview.metadata.id)} · ${escapeHtml(preview.metadata.description)}</p>
        ${existing ? localize`<p role="status">此范围已有 v${escapeHtml(existing.strategy_package.version)}${existing.locally_modified ? localize("，包含本地修改") : ""}。请比较内容后选择；另存会生成新的策略 ID。</p>` : ""}
        ${(preview.capacity || []).map(report => localize`<section data-strategy-capacity><p role="${report.fits ? "status" : "alert"}">${report.fits ? localize("策略集合容量可用") : report.input_bound <= report.previous_input_bound ? localize("策略集合仍超限，本次减少占用可保存") : localize("策略集合容量超限，请缩短正文或取消启用后重新预览")} · ${escapeHtml(report.workspace_id || localize("全局"))} · ${escapeHtml(report.model_name)}：${escapeHtml(report.input_bound)} / ${escapeHtml(report.strategy_input_limit ?? localize("不限"))}（UTF-8 序列化保守输入上界）</p><ul>${(report.strategies || []).map(item => `<li>${escapeHtml(item.title)} (${escapeHtml(item.package_id || item.memory_id)})：${escapeHtml(item.input_bound)}</li>`).join("")}</ul><p class="muted">容量统计为目录与适用信息，演化过程和历史继承按需读取。已为 Patrol 提示词、输出格式和其他观察内容保留空间；实际请求仍会完整检查容量。</p></section>`).join("")}
        <div class="strategy-import-comparison">${existing ? localize`<section><h4>当前安装</h4>${Object.entries(existing.strategy).map(([key, value]) => `<h5>${labels[key]}</h5><pre class="memory-detail-content">${escapeHtml(value)}</pre>`).join("")}</section>` : ""}<section><h4>待导入</h4>${Object.entries(preview.strategy).map(([key,value]) => `<h5>${labels[key]}</h5><pre class="memory-detail-content">${escapeHtml(value)}</pre>`).join("")}</section></div>
        <button type="button" class="primary" data-action="install-strategy"${(preview.capacity || []).some(report => !report.fits && report.input_bound > report.previous_input_bound) ? " disabled" : ""}>${existing ? localize("使用导入内容替换当前安装") : localize("保存并安装")}</button>${existing ? localize('<button type="button" data-action="fork-imported-strategy">另存为新策略并编辑</button>') : ""}</div>` : ""}</fieldset></section>`;
  }

  function render(memories, options = {}) {
    const strategies = options.category === "evolution_strategy";
    if (options.strategyImport) return `<section class="memory-view">${strategyImport(options.strategyImport, options.workspaces || [])}</section>`;
    return localize`<section class="memory-view">
      ${options.composing ? "" : localize`<nav aria-label="记忆分类">${[["", localize("全部")], ["memory", localize("普通记忆")], ["evolution_strategy", localize("Strategy · 工作策略")]].map(([category, label]) => `<button data-action="memory-category" data-category="${category}" aria-pressed="${(options.category || "") === category}">${label}</button>`).join("")}</nav>`}
      <header class="memory-page-heading"><div><h1>${strategies ? localize("Strategy · 工作策略") : localize("记忆库")}</h1><p>${strategies ? localize("你只管目标与反馈，让 Focus 按你的方法组织工作。") : localize("让重要的决定留下来，让重复的解释少一点。")}</p></div><span><button data-action="import-strategy">导入策略</button><button data-action="new-strategy"${strategies ? ' class="primary"' : ""}>新建策略</button>${strategies ? "" : localize('<button class="primary" data-action="new-memory" data-i18n="memory.create">新建记忆</button>')}</span></header>
      ${options.composing ? "" : localize`<div class="library-toolbar"><label class="page-search">${strategies ? localize("检索已安装策略") : localize("检索已加载记忆")} <input type="search" data-page-search="memory" value="${escapeHtml(options.query || "")}" placeholder="${strategies ? localize("搜索方法与适用场景…") : localize("搜索决定与约定…")}"></label><div class="library-layout" role="group" aria-label="记忆布局" data-i18n-aria-label="view.layout">${["cards", "list"].map(layout => `<button data-action="set-library-layout" data-library="memory" data-layout="${layout}" aria-pressed="${(options.layout || "cards") === layout}" data-i18n="view.${layout}">${layout === "cards" ? localize("卡片") : localize("列表")}</button>`).join("")}</div></div>`}
      ${options.loading ? localize('<p role="status">正在读取记忆…</p>') : ""}
      ${options.error ? localize`<p role="alert">读取失败：${escapeHtml(options.error)} <button data-action="refresh-memory">重试</button></p>` : ""}
      ${options.composing ? (options.draft?.category === "evolution_strategy" ? strategyEditor(options.draft, options.workspaces || [], options.authoring) : compose(options)) : ((options.loading || options.error) && !memories?.length ? "" : workbench(memories, options))}
    </section>`;
  }

  return { render, strategyText, messageText, messagePreview, roleLabel, isDialogueRole };
});
