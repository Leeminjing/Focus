/* 本文件对外提供 mount / get / leave 的会话 Patrol 工作台组合端口。
 * 输入为草稿、API 和目录；输出为稳定 identity 编辑器、虚拟列表、独立保存、来源及可追溯请求预览。
 * 工作流为字段事件局部更新，结构事件 reconcile 最多 100 cards；文档、字段和内容块在 Worker 解析及校验。
 * 字段按编辑基线合并，冲突保留 buffer；entry epoch 与请求身份拒绝旧生命周期结果。来源状态独立只读刷新。
 * 高级视图先呈现 pending 再由 Worker 格式化全文，视图/文档版本保护迟到结果；本地反馈与完整内容就绪分别计量。
 * 基础行为与 entry 编辑独立；结构 JSON 通过 JsonEditor 端口保留全文与 history，仅排版可见区域。
 * 输入法期间暂缓保存，导航保留本地 buffer；服务器版本冲突保留本地稿，显式载入可撤销。
 * 示例：FocusPatrolWorkbench.mount(root,{draft,api})。
 */
(function (global) {
  "use strict";
  const { PatrolDocument, DraftSaveQueue } = global.FocusPatrolDocument;
  const instances = new Map();
  const rootOwners = new WeakMap();
  const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; };
  const button = (text, action) => { const node = element("button", "text-button", text); node.type = "button"; node.addEventListener("click", action); return node; };
  class Workbench {
    constructor(options) {
      this.options = options; this.draft = options.draft; this.api = options.api; this.cards = new Map(); this.generation = 0; this.panelGeneration = 0; this.selected = null; this.composing = false;
      this.entryParses = new Map(); this.parseIdentity = 0;
      let value = this.draft.authoring_document || { schema_version:2, instructions:this.draft.system_prompt || "", entries:[], transformations:[] };
      let baseRevision=this.draft.draft_revision || 0;
      try { const local = JSON.parse(localStorage.getItem(this._storageKey())); if (local?.document?.schema_version===2 && Array.isArray(local.document.entries)) { value=local.document;baseRevision=local.serverRevision;this.localConflict=baseRevision!==this.draft.draft_revision; } } catch (_) {}
      this.doc = new PatrolDocument(value, () => this._changed());
      this.queue = new DraftSaveQueue({ revision:baseRevision,
        save:body => this.api(`/desktop/api/drafts/${this.draft.draft_id}`, { method:"PUT", body:JSON.stringify(body) }),
        snapshot:revision => {
          this._persist(); return { authoring_document:this.doc.value, draft_revision:revision, equipment:this.draft.equipment, mode:"standard" };
        }, onStatus:(status, result) => {
          if (result?.draft_revision != null) this.draft.draft_revision = result.draft_revision;
          if (status === "saved") { localStorage.removeItem(this._storageKey()); this.draft.authoring_document = this.doc.value; }
          if (this.status) this.status.textContent = ({pending:"有修改", saving:"保存中…", saved:"已保存", error:"保存失败，点击重试"})[status];
        }});
      this.queue.changed();
      this.worker = new Worker("./patrol-parser-worker.js");
      this.worker.onmessage = event => event.data.kind === "format" ? this._formatted(event.data) : event.data.request_id != null ? this._entryParsed(event.data) : this._parsed(event.data);
    }
    mount(root) {
      this._cancelRaw();
      this.events?.abort(); this.events = new AbortController(); const signal = this.events.signal;
      this.root = root; root.className = "patrol-workbench"; root.replaceChildren(); this.cards.clear();
      const toolbar = element("div", "patrol-toolbar");
      toolbar.append(button("增加条目", () => this._add()), button("清空", () => { this.doc.clear(); this.renderList(); this.select(null); }),
        button("撤销", () => this._undo(false)), button("重做", () => this._undo(true)), button("高级结构", () => this._toggleRaw()),
        button("导入来源", () => this._sources()), button("检查来源", () => this._refreshSources()), button("预览与检查", () => this.preview()), button("载入服务器版本", () => this._reloadServer()));
      this.status = element("button", "patrol-status", "自动保存"); this.status.onclick = () => this.queue.flush().catch(() => {}); toolbar.append(this.status);
      if(this.localConflict)this.status.textContent="另一窗口已更新；本地编辑已保留，可载入服务器版本或复制正文";
      this.list = element("div", "patrol-list"); this.list.setAttribute("role", "listbox"); this.list.setAttribute("aria-label", "上下文条目");
      this.list.addEventListener("scroll", () => { if (!this.scrollFrame) this.scrollFrame = requestAnimationFrame(() => { this.scrollFrame = null; this.renderList(); }); });
      this.editor = element("div", "patrol-editor"); this.panel = element("div", "patrol-panel"); this.panel.hidden = true;
      this.rawEditor ||= global.FocusPatrolJsonEditor.createJsonEditor({label:"高级文档 JSON，允许保存未完成输入"});
      this.raw = this.rawEditor.element; this.raw.hidden = true;
      this.rawState = element("p", "patrol-raw-state"); this.rawState.hidden = true; this.rawState.setAttribute("role", "status");
      this.raw.addEventListener("input", event => {
        if (event.target !== this.raw) return;
        this.doc.value.raw_buffer = this.rawEditor.value; this.doc.value.raw_error = "JSON 正在解析"; this._changed(); clearTimeout(this.parseTimer);
        this.parseTimer = setTimeout(() => this.worker.postMessage({ generation:this.generation, raw:this.rawEditor.value }), 450);
      }, {signal});
      const behavior = element("details", "patrol-behavior"); behavior.append(element("summary", "", "小兵基础行为（instructions）"));
      const instructions = element("textarea"); instructions.setAttribute("aria-label", "小兵基础行为"); instructions.value = this.doc.value.instructions || "";
      this.instructions = instructions;
      instructions.addEventListener("input", () => this.doc.setInstructions(instructions.value)); behavior.append(instructions);
      root.append(toolbar, behavior, this.list, this.editor, this.rawState, this.raw, this.panel);
      root.addEventListener("input", event => event.stopPropagation(), {signal}); root.addEventListener("change", event => event.stopPropagation(), {signal});
      root.addEventListener("compositionstart", () => { this.composing = true; this.queue.held = true; }, {signal});
      root.addEventListener("compositionend", () => {
        this.composing = false; this.queue.held = false;
        for (const state of this.entryParses.values()) if (state.result) this._entryParsed(state.result);
        if (this.pendingDocument) { const result = this.pendingDocument; this.pendingDocument = null; this._parsed(result); }
        this.queue.changed();
      }, {signal});
      root.addEventListener("keydown", event => this._keys(event), {signal});
      this.renderList(); this.select(this.selected || this.doc.value.entries[0]?.entry_id);
      this._refreshSources();
    }
    rebind(options) { this.options = options; this.draft = options.draft; }
    configurationChanged() { this._changed(); }
    _syncInstructions() { if(this.instructions)this.instructions.value=this.doc.value.instructions || ""; }
    _changed() {
      this.generation++; this.previewPlan = null; if (this.options.onStale) this.options.onStale();
      if (!this.reloading) this.queue.changed(this.composing);
      clearTimeout(this.localTimer); this.localTimer = setTimeout(() => this._persist(), 450);
    }
    _storageKey() { return "focus-patrol-draft:" + this.draft.draft_id; }
    _persist() {
      try { localStorage.setItem(this._storageKey(), JSON.stringify({ serverRevision:this.queue.serverRevision, document:this.doc.value })); } catch (_) { if (this.status) this.status.textContent = "本地缓存空间不足，草稿仍可保存"; }
    }
    leave() { this._cancelDrag?.(); this._cancelRaw(); this._persist(); this.events?.abort(); this.composing = false; this.queue.held = false; this.queue.flush().catch(() => {}); }
    async flush() { if (this.composing) throw new Error("请完成当前输入"); return this.queue.flush(); }
    async _reloadServer() {
      if (this.composing) return;
      const generation=this.generation;
      try {
        if(this.queue.running)await this.queue.running.catch(()=>{});
        const remote=await this.api(`/desktop/api/drafts/${this.draft.draft_id}`);
        if(generation!==this.generation){this.status.textContent="载入期间有新编辑，已保留本地内容";return;}
        clearTimeout(this.queue.timer);this.queue.timer=null;Object.assign(this.draft,remote);
        this.reloading=true;this.doc.replace(remote.authoring_document);this.reloading=false;
        this.queue.serverRevision=remote.draft_revision;this.queue.confirmed=this.queue.localRevision;this.queue.error=null;this.localConflict=false;
        clearTimeout(this.localTimer);localStorage.removeItem(this._storageKey());this.editorId=undefined;this.renderList();this.select(this.selected);
        this._syncInstructions();this.status.textContent="已载入服务器版本；可以撤销恢复刚才的本地内容";this.options.onReload?.(remote);
      } catch(error){this.reloading=false;this.status.textContent=error.message;}
    }
    renderList() {
      const entries = this.doc.value.entries, start = Math.max(0, Math.floor(this.list.scrollTop / 72) - 10), end = Math.min(entries.length, start + 100);
      const wanted = new Set(entries.slice(start, end).map(e => e.entry_id));
      for (const [id, card] of this.cards) if (!wanted.has(id)) { card.remove(); this.cards.delete(id); }
      if (!this.topSpacer) { this.topSpacer = element("div"); this.bottomSpacer = element("div"); }
      this.topSpacer.style.height = `${start * 72}px`; this.bottomSpacer.style.height = `${(entries.length - end) * 72}px`;
      const desired = [this.topSpacer];
      for (let index = start; index < end; index++) {
        const entry = entries[index]; let card = this.cards.get(entry.entry_id);
        if (!card) {
          card = element("div", "patrol-entry"); card.dataset.entryId = entry.entry_id; card.tabIndex = 0; card.setAttribute("role", "option");
          const handle = button("↕", () => {}); handle.setAttribute("aria-label", "拖动排序"); handle.addEventListener("pointerdown", event => this._drag(event, entry.entry_id));
          card.append(handle, element("span", "patrol-entry-text")); card.onclick = () => this.select(entry.entry_id); this.cards.set(entry.entry_id, card);
        }
        card.setAttribute("aria-selected", String(entry.entry_id === this.selected));
        card.entryIndex = index; const summary = this._entrySummary(entry, index);
        if (card.lastChild.textContent !== summary) card.lastChild.textContent = summary;
        desired.push(card);
      }
      desired.push(this.bottomSpacer);
      desired.forEach((node, index) => { if (this.list.children[index] !== node) this.list.insertBefore(node, this.list.children[index] || null); });
    }
    select(id) {
      if (this.composing || this.editorId === id && this.editor.childNodes.length) return;
      this.selected = id || null; this.editorId = id; this.editor.replaceChildren(); this.renderList();
      const entry = this.doc.byId.get(id); if (!entry) { this.editor.append(element("p", "muted", "可以从空白文档开始，选择条目后编辑。")); return; }
      const tools = element("div", "patrol-actions"), role = element("input"); role.setAttribute("list", "patrol-role-options"); role.setAttribute("aria-label", "角色意图"); role.value = entry.role;
      const roles = element("datalist"); roles.id = "patrol-role-options"; ["system", "developer", "user", "assistant", "tool"].forEach(value => { const option = element("option"); option.value = value; roles.append(option); });
      role.oninput = () => { this.doc.setField(id, "role", role.value); this._summary(id); };
      tools.append(role, roles, button("复制", () => { const copied = this.doc.copy(id); this.renderList(); this.select(copied.entry_id); }),
        button("删除", () => { this.doc.remove(id); this.renderList(); this.select(null); }), button("向上", () => this._moveSelected(-1)), button("向下", () => this._moveSelected(1)));
      this.editor.append(tools);
      const text = element("textarea"); text.setAttribute("aria-label", "条目正文"); text.value = typeof entry.content === "string" ? entry.content : JSON.stringify(entry.content, null, 2);
      if (entry.content_error && entry.content_buffer != null) text.value = entry.content_buffer;
      text.oninput = () => {
        if (typeof entry.content === "string") { this.doc.setField(id, "content", text.value); this._summary(id); }
        else {
          this.doc.setField(id, "content_buffer", text.value); this.doc.setField(id, "content_error", "待解析内容块");
          this._parseEntry(entry, "content", text.value, { content:entry.content });
        }
      };
      this.editor.append(text);
      const detail = element("details"), summary = element("summary", "", "工具字段与未知结构（可自由编辑）"); detail.append(summary);
      detail.addEventListener("toggle", () => {
        if (!detail.open || detail.children.length > 1) return;
        const fields = element("textarea"); fields.setAttribute("aria-label", "工具和扩展字段 JSON"); fields.value = entry.fields_error && entry.fields_buffer != null ? entry.fields_buffer : JSON.stringify(entry, null, 2);
        fields.wrap = "off"; fields.spellcheck = false;
        let base = entry.fields_base || structuredClone(entry);
        fields.oninput = () => {
          entry.fields_base = base; entry.fields_buffer = fields.value; entry.fields_error = "待解析字段"; this._changed();
          this._parseEntry(entry, "fields", fields.value, base, value => { base = value; });
        }; detail.append(fields);
      });
      this.sourceLabel = element("p", "muted"); this.editor.append(detail, this.sourceLabel); this._sourceLabel();
    }
    _parseEntry(entry, kind, raw, base, applied) {
      const key = `${entry.entry_id}:${kind}`; clearTimeout(this.entryParses.get(key)?.timer);
      const state = { entry, kind, base, applied, epoch:this.doc.versionOf(entry.entry_id), request_id:++this.parseIdentity };
      state.timer = setTimeout(() => this.worker.postMessage({ request_id:state.request_id, entry_id:entry.entry_id, kind, raw }), 450);
      this.entryParses.set(key, state);
    }
    _entryParsed(result) {
      const key = `${result.entry_id}:${result.kind}`, state = this.entryParses.get(key);
      if (!state || state.request_id !== result.request_id) return;
      if (this.composing) { state.result = result; return; }
      this.entryParses.delete(key);
      const entry = this.doc.byId.get(result.entry_id);
      if (entry !== state.entry || this.doc.versionOf(result.entry_id) !== state.epoch) return;
      let error = result.error, changes = [];
      if (!error) {
        const value = result.kind === "content" ? { content:result.value } : { role:"user", content:"", ...result.value };
        const pendingContent = this.entryParses.has(`${entry.entry_id}:content`) || entry.content_error && entry.content_buffer != null;
        const merged = this.doc.applyFields(entry.entry_id, state.base, value, { pendingFields:result.kind === "fields" && pendingContent ? ["content"] : [] });
        error = merged.conflicts.length ? `字段已被较新编辑修改：${merged.conflicts.join("、")}；原输入已保留` : null;
        changes = merged.changed;
        if (!error) state.applied?.(value);
      }
      this.doc.setField(entry.entry_id, `${result.kind}_error`, error);
      if (!error) { delete entry[`${result.kind}_buffer`]; if (result.kind === "fields") delete entry.fields_base; }
      if (this.selected === entry.entry_id) {
        const text = this.editor.querySelector('[aria-label="条目正文"]'), role = this.editor.querySelector('[aria-label="角色意图"]');
        if (result.kind === "fields" && changes.includes("content") && text) text.value = typeof entry.content === "string" ? entry.content : JSON.stringify(entry.content, null, 2);
        if (changes.includes("role") && role) role.value = entry.role;
        this._summary(entry.entry_id); this._sourceLabel();
        if (error) this.status.textContent = error;
      }
    }
    _sourceLabel() {
      const entry = this.doc.byId.get(this.selected); if (!entry || !this.sourceLabel?.isConnected) return;
      const health = this.sourceStatus?.[entry.source_ref];
      const status = this.sourceChecking ? " · 正在检查原来源" : this.sourceFailed ? " · 暂时无法检查来源，冻结正文仍可编辑" :
        health?.status === "unavailable" ? ` · 原来源不可用（${health.message}），冻结正文仍可编辑` : health?.status === "unknown" ? " · 旧引用状态无法检查" : "";
      this.sourceLabel.textContent = entry.source_ref ? `来源 ${entry.source_ref}${entry.edited_from ? " · 用户改写" : " · 冻结引用"}${status}` : "用户编写 · 历史示例不登记为真实工具执行";
    }
    async _refreshSources() {
      const ticket = this.healthGeneration = (this.healthGeneration || 0) + 1;
      this.sourceChecking = true; this.sourceFailed = false; this._sourceLabel();
      try {
        const health = await this.api(`/desktop/api/drafts/${this.draft.draft_id}/source-status`);
        if (ticket !== this.healthGeneration || !this.root.isConnected) return;
        this.sourceStatus = health.sources; this.sourceChecking = false; this._sourceLabel(); this.renderList();
      } catch (_) { if (ticket === this.healthGeneration) { this.sourceStatus = null; this.sourceChecking = false; this.sourceFailed = true; this._sourceLabel(); } }
    }
    _summary(id) {
      const card = this.cards.get(id), entry = this.doc.byId.get(id); if (card) card.lastChild.textContent = this._entrySummary(entry, card.entryIndex);
    }
    _entrySummary(entry, index) {
      return `${index + 1} · ${entry.role} · ${typeof entry.content === "string" ? entry.content.slice(0, 120) : "结构化内容块"}${entry.source_ref ? " · 引用" : ""}${this.sourceStatus?.[entry.source_ref]?.status === "unavailable" ? " · 原来源不可用" : ""}`;
    }
    _add() { const entry = { entry_id:crypto.randomUUID(), role:"user", content:"" }; this.doc.insert(entry); this.renderList(); this.select(entry.entry_id); }
    _moveSelected(delta) { if (!this.selected) return; const index = this.doc.value.entries.findIndex(e => e.entry_id === this.selected); this.doc.move(this.selected, index + delta); this.renderList(); }
    _undo(redo) { if (this.composing) return; (redo ? this.doc.redo() : this.doc.undo()); this._syncInstructions();this.editorId = undefined; this.renderList(); this.select(this.selected); }
    _keys(event) {
      if (event.key === "Escape") { this._cancelDrag?.(); this.panelGeneration++; this.panel.hidden = true; return; }
      if (event.target.dataset.entryId && ["Enter", " "].includes(event.key)) { event.preventDefault(); this._locate(event.target.dataset.entryId); }
      if (event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) { event.preventDefault(); this._moveSelected(event.key === "ArrowUp" ? -1 : 1); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !["TEXTAREA", "INPUT"].includes(event.target.tagName) && !this.raw.contains(event.target)) { event.preventDefault(); this._undo(event.shiftKey); }
    }
    async _toggleRaw() {
      if (this.composing) return;
      this._cancelRaw();
      this.raw.hidden = !this.raw.hidden; this.list.hidden = !this.raw.hidden; this.editor.hidden = !this.raw.hidden;
      this.rawState.hidden = this.raw.hidden;
      if (this.raw.hidden) return false;
      this.rawState.textContent = "正在准备完整结构…"; this.rawEditor.readOnly = true; this.rawEditor.refresh(); this.rawEditor.focus();
      return new Promise(resolve => {
        const request = this.rawFormat = { view_id:this.rawViewGeneration, resolve };
        requestAnimationFrame(() => requestAnimationFrame(() => { if (this.rawFormat === request) this._requestRaw(request); }));
      });
    }
    _cancelRaw() {
      this.rawViewGeneration = (this.rawViewGeneration || 0) + 1;
      this.rawFormat?.resolve(false); this.rawFormat = null;
    }
    _requestRaw(request) {
      const {raw_buffer, raw_error, ...value} = this.doc.value; request.generation = this.generation;
      this.worker.postMessage({ kind:"format", view_id:request.view_id, generation:request.generation, raw:raw_error || this.rawDocumentGeneration === this.generation ? raw_buffer : null, value });
    }
    _formatted(result) {
      const request = this.rawFormat;
      if (!request || result.view_id !== request.view_id || this.raw.hidden) return;
      if (result.generation !== this.generation) { this._requestRaw(request); return; }
      const focused = this.rawEditor.hasFocus;
      this.rawFormat = null; this.rawEditor.readOnly = false;
      if (result.error) this.rawState.textContent = result.error;
      else { this.rawEditor.value = result.raw; this.rawState.hidden = true; this.rawEditor.refresh(); if (focused) this.rawEditor.focus(); }
      request.resolve(!result.error);
    }
    _parsed(result) {
      if (result.generation !== this.generation) return;
      if (this.composing) { this.pendingDocument = result; return; }
      const raw = this.rawEditor.value;
      const invalid = result.error || global.FocusPatrolAuthoring.validateDocument(result.value);
      if (invalid) {
        this.doc.value.raw_error = invalid; this._changed(); this.rawDocumentGeneration = this.generation; this.status.textContent = invalid; return;
      }
      const value = result.value;
      value.entries = value.entries.map(entry => ({role:"user", content:"", ...entry, entry_id:entry.entry_id || crypto.randomUUID()}));
      value.raw_buffer = raw; value.raw_error = null;
      this.doc.replace(value);this.rawDocumentGeneration = this.generation;this._syncInstructions(); this.editorId = undefined; this.renderList(); this.select(this.selected);
    }
    async preview() {
      const ticket = ++this.panelGeneration;
      this.panel.hidden = false; this.panel.textContent = "正在准备实际请求…";
      try {
        await this.flush(); const generation = this.generation;
        const plan = await this.api(`/desktop/api/drafts/${this.draft.draft_id}/preview`, { method:"POST" });
        if (generation !== this.generation || ticket !== this.panelGeneration || !this.root.isConnected) return null;
        this.previewPlan = plan; this.panel.replaceChildren();
        this.panel.append(element("p", "", plan.budget ? `窗口：${plan.budget.total} / ${plan.budget.context_window || "未知"}；输入 ${plan.budget.input}，工具 ${plan.budget.tools}，指令 ${plan.budget.instructions}，输出预留 ${plan.budget.output_reserve}` : "检查以下条目"));
        for (const diagnostic of plan.diagnostics) {
          const row = element("div", "patrol-diagnostic"); row.append(element("span", "", diagnostic.message), button("定位", () => this._locate(diagnostic.entry_ids[0])));
          if (diagnostic.entry_ids.length) row.append(button("转参考文本", () => this._transform(plan, diagnostic, "as_text")), button("未完成结果", () => this._transform(plan, diagnostic, "placeholder")));
          this.panel.append(row);
        }
        const details = element("details"), summary = element("summary", "", "实际 instructions / input / tools 与角色映射"); details.append(summary);
        details.addEventListener("toggle", () => { if (details.open && details.children.length === 1) details.append(element("pre", "", JSON.stringify({ request:plan.request, roles:plan.role_mappings, sources:plan.items?.map(item=>({entry:item.message_id,refs:item.source_refs})), transformations:plan.transformations }, null, 2))); });
        this.panel.append(details); if (this.options.onPreview) this.options.onPreview(plan); return plan;
      } catch (error) { if (ticket === this.panelGeneration && this.root.isConnected) this.panel.textContent = error.message; return null; }
    }
    _transform(plan, diagnostic, operation) { this.doc.transform({ document_hash:plan.document_hash, entry_ids:diagnostic.entry_ids, operation, parameters:{} }); this.preview(); }
    _locate(id) { const index = this.doc.value.entries.findIndex(e => e.entry_id === id); if (index < 0) return; this.list.scrollTop = index * 72; this.renderList(); this.select(id); this.editor.querySelector("textarea[aria-label='条目正文']")?.focus(); }
    async _sources() {
      const ticket = ++this.panelGeneration;
      this.panel.hidden = false; this.panel.textContent = "读取来源版本…"; const generation = this.sourceGeneration = (this.sourceGeneration || 0) + 1;
      try {
        const catalog = await this.api("/desktop/api/patrol/sources"); if (generation !== this.sourceGeneration || ticket !== this.panelGeneration || !this.root.isConnected) return;
        this.panel.replaceChildren();
        const select = element("select"); select.setAttribute("aria-label", "精确来源版本");
        const refs = [];
        for (const context of catalog.contexts) for (const revision of context.revisions) refs.push({ title:`${context.title} · R${revision.generation} · ${revision.checkpoint_id || "definition"}`, kind:"context", revision_id:revision.revision_id, context_id:context.context_id });
        for (const branch of catalog.branches) if (branch.checkpoint_id) refs.push({ ...branch, title:`小兵 ${branch.run_id} · ${branch.checkpoint_id}`, kind:"patrol" });
        refs.forEach((ref, index) => { const option = element("option", "", ref.title); option.value = String(index); select.append(option); });
        const historical = element("input"); historical.type = "checkbox"; const label = element("label", "", "包括历史运行参考"); label.prepend(historical);
        this.panel.append(select, label, button("追加选定版本", () => this._import({ ...refs[Number(select.value)], include_historical_runtime:historical.checked })));
        const path = element("input"); path.placeholder = "相对工作区的文件路径"; path.setAttribute("aria-label", "冻结文件路径");
        this.panel.append(path, button("冻结文件", () => this._import({ kind:"file", context_id:this.draft.task_id, path:path.value })));
        const materials = await this.api(`/desktop/api/tasks/${this.draft.task_id}/materials`);
        if (generation !== this.sourceGeneration || ticket !== this.panelGeneration || !this.root.isConnected) return;
        for (const material of materials) this.panel.append(button(`材料 ${material.relative_path}`, async () => {
          const versions = await this.api(`/desktop/api/materials/${material.material_id}/versions`);
          if (generation !== this.sourceGeneration || ticket !== this.panelGeneration || !this.root.isConnected) return;
          const versionSelect = element("select"); const current = element("option", "", "当前文件快照"); current.value = ""; versionSelect.append(current);
          versions.forEach(version => { const option = element("option", "", version.version_id); option.value = version.version_id; versionSelect.append(option); });
          this.panel.append(versionSelect, button("追加材料", () => this._import({ kind:"material", context_id:this.draft.task_id, material_id:material.material_id, version_id:versionSelect.value || null })));
        }));
      } catch (error) { if (ticket === this.panelGeneration && this.root.isConnected) this.panel.textContent = error.message; }
    }
    async _import(ref) {
      if (this.composing) return;
      try {
        const generation = this.generation;
        await this.queue.mutate(async () => {
          const result = await this.api(`/desktop/api/drafts/${this.draft.draft_id}/sources`, { method:"POST", body:JSON.stringify({ ...ref, draft_revision:this.queue.serverRevision }) });
          this.queue.serverRevision = result.draft_revision; this.draft.draft_revision = result.draft_revision;
          this.doc.append(result.authoring_document.entries);
        });
        this.renderList(); this.panel.textContent = generation === this.generation ? "来源已冻结" : "来源已追加，保留本地编辑";
        this._refreshSources();
      } catch (error) { this.panel.textContent = error.message; }
    }
    _drag(event, id) {
      event.preventDefault(); const listRect = this.list.getBoundingClientRect(); this.drag = { id, y:event.clientY, index:0 };
      const move = e => { if (this.drag) this.drag.y = e.clientY; };
      const tick = () => {
        if (!this.drag) return;
        const y = this.drag.y; if (y < listRect.top + 40) this.list.scrollTop -= 12; else if (y > listRect.bottom - 40) this.list.scrollTop += 12;
        this.drag.index = Math.max(0, Math.min(this.doc.value.entries.length - 1, Math.floor((y - listRect.top + this.list.scrollTop) / 72)));
        this.renderList(); const target = this.doc.value.entries[this.drag.index]?.entry_id;
        for (const [entryId, card] of this.cards) card.classList.toggle("patrol-drop", entryId === target);
        this.dragFrame = requestAnimationFrame(tick);
      };
      const finish = e => {
        document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", finish); document.removeEventListener("pointercancel", finish); cancelAnimationFrame(this.dragFrame);
        const drag = this.drag; this.drag = null; if (drag && e.type === "pointerup") this.doc.move(drag.id, drag.index);
        for (const card of this.cards.values()) card.classList.remove("patrol-drop"); this.renderList();
        this._cancelDrag = null;
      };
      this._cancelDrag = () => finish({type:"pointercancel"});
      document.addEventListener("pointermove", move); document.addEventListener("pointerup", finish); document.addEventListener("pointercancel", finish); tick();
    }
  }
  global.FocusPatrolWorkbench = {
    mount(root, options) {
      let instance = instances.get(options.draft.draft_id);
      if (!instance) { instance = new Workbench(options); instances.set(options.draft.draft_id, instance); }
      else {
        instance.rebind(options);
        if(options.draft.draft_revision!==instance.queue.serverRevision){
          if(instance.queue.confirmed<instance.queue.localRevision||instance.composing)instance.localConflict=true;
          else {instance.reloading=true;instance.doc.replace(options.draft.authoring_document);instance.reloading=false;instance.queue.serverRevision=options.draft.draft_revision;}
        }
      }
      const previous = rootOwners.get(root); if(previous && previous !== instance)previous.leave();
      rootOwners.set(root,instance); root.dataset.draftId=options.draft.draft_id; instance.mount(root); return instance;
    },
    get(id) { return instances.get(id); }, leave(id) { instances.get(id)?.leave(); },
  };
})(window);
