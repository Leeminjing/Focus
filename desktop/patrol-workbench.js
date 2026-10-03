/* 本文件对外提供 FocusPatrolWorkbench.mount/get/leave 的会话工作台组合端口。
 * 输入为真实挂载节点、v3 草稿、API 和状态回调；输出为唯一作者文档、保存队列及模块化左右编排界面。
 * 具体工作流为恢复本地 buffer → 组合来源/目标/交互/检查视图 → 串行保存与字段 epoch 合并；离开释放观察器/worker，保留有界字段历史和解析意图，重开继续解析。
 * 来源控制器不拥有作者文档，诊断/预算由真实 preview 产生；输入法期间保留意图，不因异步结果重建编辑器。
 * 示例：const wb=FocusPatrolWorkbench.mount(root,{draft,api}); await wb.flush(); await wb.preview()。
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
      let value = this.draft.authoring_document || { schema_version:3, instructions:this.draft.system_prompt || "", entries:[], transformations:[] };
      let baseRevision=this.draft.draft_revision || 0;
      try { const local = JSON.parse(localStorage.getItem(this._storageKey())); if ([2,3].includes(local?.document?.schema_version) && Array.isArray(local.document.entries)) { value=local.document;baseRevision=local.serverRevision;this.localConflict=baseRevision!==this.draft.draft_revision; } } catch (_) {}
      this.doc = new PatrolDocument(global.FocusPatrolAuthoring.upgradeDocument(value), () => this._changed());
      this.queue = new DraftSaveQueue({ revision:baseRevision,
        save:body => this.api(`/desktop/api/drafts/${this.draft.draft_id}`, { method:"PUT", body:JSON.stringify(body) }),
        snapshot:revision => {
          this._persist(); return { authoring_document:this.doc.value, draft_revision:revision, equipment:this.draft.equipment, mode:"standard" };
        }, onStatus:(status, result) => {
          if (result?.draft_revision != null) this.draft.draft_revision = result.draft_revision;
          if (status === "saved") { localStorage.removeItem(this._storageKey()); this.draft.authoring_document = this.doc.value; }
          if (this.status) this.status.textContent = ({pending:"有修改", saving:"保存中…", saved:"已保存", error:"保存失败，点击重试"})[status];
          if(status==="error" && (result?.code || result?.detail?.code)==="draft_revision_conflict"){
            this.queue.blocked=result;this.localConflict=true;if(this.status)this.status.textContent="保存冲突 · 本地编辑已保留，请载入服务器版本（可撤销）";
          }
        }});
      this.queue.changed();
      this.sourceController=new global.FocusPatrolSource.PatrolSourceController({draft:this.draft,api:(...args)=>this.api(...args)});
      this.interactions=new global.FocusPatrolInteractions.PatrolInteractions(this);
      this.review=new global.FocusPatrolReview.PatrolReviewPanel(this);
    }
    _startWorker(){
      if(this.worker)return;this.worker=new Worker("./patrol-parser-worker.js");
      this.worker.onmessage=event=>event.data.kind==="format"?this._formatted(event.data):event.data.request_id!=null?this._entryParsed(event.data):this._parsed(event.data);
      for(const state of this.entryParses.values())if(state.result)this._entryParsed(state.result);else this.worker.postMessage({request_id:state.request_id,entry_id:state.entry.entry_id,kind:state.kind,raw:state.raw});
      if(this.doc.value.raw_error==="JSON 正在解析")this.worker.postMessage({generation:this.generation,raw:this.doc.value.raw_buffer});
    }
    _stopWorker(){clearTimeout(this.parseTimer);for(const state of this.entryParses.values())clearTimeout(state.timer);this.worker?.terminate();this.worker=null;}
    mount(root) {
      this._cancelRaw();this.interactions.cancel();this.events?.abort();this.events=new AbortController();const signal=this.events.signal;
      this.sourceView?.destroy();this.view?.destroy();this.root=root;this.active=true;this.cards.clear();
      this.rawEditor ||= global.FocusPatrolJsonEditor.createJsonEditor({label:"高级文档 JSON，允许保存未完成输入"});
      const previous=this.viewport;previous?.suspend();
      this.view=new global.FocusPatrolAssembly.PatrolAssemblyView(this,root);
      for(const key of ["status","list","panel","raw","rawState","instructions"])this[key]=this.view[key];
      this.viewport=new global.FocusPatrolViewport.PatrolViewport(this,this.list);
      if(previous){this.viewport.cache=previous.cache;this.viewport.heights=previous.heights;this.viewport.widths=previous.widths;this.restoredScroll=previous.list.scrollTop;}
      this.sourceView=new global.FocusPatrolSourceBrowser.PatrolSourceBrowser(this.sourceController,{onInsert:()=>this.interactions.importSelected(),onDrag:event=>this.interactions.startSource(event)});
      this.view.source.append(this.sourceView.node);this.review.bind(this.view);
      this._startWorker();
      this.raw.addEventListener("input",event=>{
        if(event.target!==this.raw)return;this.doc.value.raw_buffer=this.rawEditor.value;this.doc.value.raw_error="JSON 正在解析";this._changed();clearTimeout(this.parseTimer);
        this.parseTimer=setTimeout(()=>this.worker.postMessage({generation:this.generation,raw:this.rawEditor.value}),450);
      },{signal});
      root.addEventListener("input",event=>event.stopPropagation(),{signal});root.addEventListener("change",event=>event.stopPropagation(),{signal});
      root.addEventListener("compositionstart",()=>{this.composing=true;this.queue.held=true;},{signal});
      root.addEventListener("compositionend",()=>{
        this.composing=false;this.queue.held=false;for(const state of this.entryParses.values())if(state.result)this._entryParsed(state.result);
        if(this.pendingDocument){const result=this.pendingDocument;this.pendingDocument=null;this._parsed(result);}this.renderList();this.queue.changed();
      },{signal});
      root.addEventListener("keydown",event=>this._keys(event),{signal});
      this.renderList();this.select(this.selected || this.doc.value.entries[0]?.entry_id);
      if(this.restoredScroll!=null){this.list.scrollTop=this.restoredScroll;this.restoredScroll=null;this.renderList();}
      if(this.localConflict)this.status.textContent="本地编辑已保留 · 服务器有新版本";
      if(!this.sourceController.catalog.length && this.sourceController.kind!=="file")this.sourceController.directory();
      this._refreshSources();
    }
    rebind(options) { this.options = options; this.draft = options.draft; this.sourceController.draft=this.draft; }
    configurationChanged() { this._changed(); }
    _syncInstructions() { if(this.instructions)this.instructions.value=this.doc.value.instructions || ""; }
    _changed() {
      this.generation++; this.review?.invalidate(); this.previewPlan = null; if (this.options.onStale) this.options.onStale();
      if (!this.reloading) this.queue.changed(this.composing);
      clearTimeout(this.localTimer); this.localTimer = setTimeout(() => this._persist(), 450);
    }
    _storageKey() { return "focus-patrol-draft:" + this.draft.draft_id; }
    _persist() {
      try { localStorage.setItem(this._storageKey(), JSON.stringify({ serverRevision:this.queue.serverRevision, document:this.doc.value })); } catch (_) { if (this.status) this.status.textContent = "本地缓存空间不足，草稿仍可保存"; }
    }
    leave() { this.active=false;this._cancelDrag?.(); this._cancelRaw(); this._persist();clearTimeout(this.localTimer);this._stopWorker();this.events?.abort();this.viewport?.suspend();this.sourceView?.destroy();this.view?.destroy();this.review.destroy();this.healthGeneration=(this.healthGeneration || 0)+1;
      this.composing = false; this.queue.held = false; this.queue.flush().catch(() => {}); }
    async flush() { if (this.composing) throw new Error("请完成当前输入"); return this.queue.flush(); }
    async _reloadServer() {
      if (this.composing) return;
      const generation=this.generation;
      try {
        if(this.queue.running)await this.queue.running.catch(()=>{});
        const remote=await this.api(`/desktop/api/drafts/${this.draft.draft_id}`);
        if(generation!==this.generation){this.status.textContent="载入期间有新编辑，已保留本地内容";return;}
        clearTimeout(this.queue.timer);this.queue.timer=null;Object.assign(this.draft,remote);
        this.reloading=true;this.doc.replace(global.FocusPatrolAuthoring.upgradeDocument(remote.authoring_document));this.reloading=false;
        this.queue.serverRevision=remote.draft_revision;this.queue.confirmed=this.queue.localRevision;this.queue.error=null;this.queue.blocked=null;this.localConflict=false;
        clearTimeout(this.localTimer);localStorage.removeItem(this._storageKey());this.renderList();this.select(this.selected);
        this._syncInstructions();this.status.textContent="已载入服务器版本；可以撤销恢复刚才的本地内容";this.options.onReload?.(remote);
      } catch(error){this.reloading=false;this.status.textContent=error.message;}
    }
    renderList() { if(!this.active)return;this.viewport.render(); this.view?.update(); }
    select(id) { this.selected=id || null;this.renderList();this.editor=this.cards.get(id) || this.list; }
    _parseEntry(entry, kind, raw, base, applied, field = "content") {
      const key = `${entry.entry_id}:${kind}`; clearTimeout(this.entryParses.get(key)?.timer);
      const state = { entry, kind, raw, base, applied, field, epoch:this.doc.versionOf(entry.entry_id), request_id:++this.parseIdentity };
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
        const value = result.kind === "content" ? { ...state.base, payload:{...state.base.payload,[state.field || "content"]:result.value} } : result.kind === "payload" ? {...state.base,payload:result.value} : result.value;
        const pendingContent = this.entryParses.has(`${entry.entry_id}:content`) || entry.content_error && entry.content_buffer != null;
        const merged = this.doc.applyFields(entry.entry_id, state.base, value, { pendingFields:result.kind === "fields" && pendingContent ? ["payload.content", "payload.output"] : [] });
        error = merged.conflicts.length ? `字段已被较新编辑修改：${merged.conflicts.join("、")}；原输入已保留` : null;
        changes = merged.changed;
        if (!error) state.applied?.(value);
      }
      this.doc.setField(entry.entry_id, `${result.kind}_error`, error);
      if (!error) { delete entry[`${result.kind}_buffer`]; if (result.kind === "fields") delete entry.fields_base; }
      if(changes.includes("kind"))this.renderList();
      this.viewport.cache.get(entry.entry_id)?.sync(this.doc.value.entries.indexOf(entry));
      if(error)this.status.textContent=error;
    }
    _sourceLabel() {for(const [id,card] of this.viewport.cache)if(card.node.isConnected)card.sync(this.doc.value.entries.indexOf(this.doc.byId.get(id)));}
    async _refreshSources() {
      const ticket = this.healthGeneration = (this.healthGeneration || 0) + 1;
      this.sourceChecking = true; this.sourceFailed = false; this._sourceLabel();
      try {
        const health = await this.api(`/desktop/api/drafts/${this.draft.draft_id}/source-status`);
        if (ticket !== this.healthGeneration || !this.root.isConnected) return;
        this.sourceStatus = health.sources; this.sourceChecking = false; this._sourceLabel(); this.renderList();
      } catch (_) { if (ticket === this.healthGeneration) { this.sourceStatus = null; this.sourceChecking = false; this.sourceFailed = true; this._sourceLabel(); } }
    }
    _add(kind="message",role="user",index) { this.interactions.run(()=>{const entry=global.FocusPatrolAuthoring.newEntry(kind,role);const anchor=this.interactions.anchor;
      if(index==null && anchor!=null){index=this.doc.value.entries.findIndex(value=>value.entry_id===anchor);if(index<0){this.status.textContent="插入位置已不存在，请重新选择位置";return;}}
      this.doc.insert(entry,index);this.renderList();this._locate(entry.entry_id);}); }
    _moveSelected(delta) { this.interactions.run(()=>{if(!this.selected)return;const index=this.doc.value.entries.findIndex(entry=>entry.entry_id===this.selected);this.doc.move(this.selected,index+delta);this.renderList();}); }
    _undo(redo) {
      if(this.composing)return;this.interactions.run(()=>{
        const owned=this.root.contains(document.activeElement);(redo?this.doc.redo():this.doc.undo());this._syncInstructions();this.renderList();this.select(this.selected);
        if(owned && !this.root.contains(document.activeElement)){this.list.tabIndex=-1;this.list.focus({preventScroll:true});}
      });
    }
    _keys(event) {
      if(event.defaultPrevented)return;
      if (event.key === "Escape") { this._cancelDrag?.(); this.review.close(); return; }
      if (event.target.dataset.entryId && ["Enter", " "].includes(event.key)) { event.preventDefault(); this._locate(event.target.dataset.entryId); }
      if (event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) { event.preventDefault(); this._moveSelected(event.key === "ArrowUp" ? -1 : 1); }
      if(event.target.closest?.('input, textarea, [contenteditable], [data-patrol-text-editor]'))return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); this._undo(event.shiftKey); }
    }
    async _toggleRaw() {
      if (this.composing) return;
      this._cancelRaw();
      this.raw.hidden = !this.raw.hidden; this.list.hidden = !this.raw.hidden;
      this.rawState.hidden = this.raw.hidden;
      if (this.raw.hidden) {
        requestAnimationFrame(()=>{for(const card of this.viewport.cache.values())if(card.node.isConnected)card.refresh();this.viewport.schedule();});
        return false;
      }
      this.panelGeneration++;this.panel.hidden=true;
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
      if(this.interactions.busy){this.interactions.run(()=>{
        if(result.generation===this.generation)this._parsed(result);
        else {this.doc.value.raw_error="导入后上下文结构已变化，完整 JSON 输入已保留，请重新编辑确认";this._changed();this.rawState.hidden=false;this.rawState.textContent=this.doc.value.raw_error;}
      });return;}
      const raw = this.rawEditor.value;
      const invalid = result.error || global.FocusPatrolAuthoring.validateDocument(result.value);
      if (invalid) {
        this.doc.value.raw_error = invalid; this._changed(); this.rawDocumentGeneration = this.generation; this.status.textContent = invalid; return;
      }
      const value = result.value;
      Object.assign(value,global.FocusPatrolAuthoring.upgradeDocument(value));value.entries=value.entries.map(entry=>({...entry,entry_id:entry.entry_id || crypto.randomUUID()}));
      value.raw_buffer = raw; value.raw_error = null;
      this.doc.replace(value);this.rawDocumentGeneration = this.generation;this._syncInstructions(); this.renderList(); this.select(this.selected);
    }
    preview() { return this.review.check(); }
    _locate(id,field=null) {
      this.selected=id;this.view.setPage("target");
      const card=this.viewport.cache.get(id);
      if(field==="fields"){this.viewport.locate(id);const details=this.cards.get(id)?.querySelector(".patrol-item-structure");if(details)details.open=true;requestAnimationFrame(()=>this.viewport.cache.get(id)?.structureEditor?.focus());}
      else this.viewport.locate(id,field?.replace(/^payload\./,""));
      this.editor=this.cards.get(id) || this.list;
    }
    _drag(event,id) { this.interactions.startTarget(event,id); }

  }
  global.FocusPatrolWorkbench = {
    mount(root, options) {
      let instance = instances.get(options.draft.draft_id);
      if (!instance) { instance = new Workbench(options); instances.set(options.draft.draft_id, instance); }
      else {
        instance.rebind(options);
        if(options.draft.draft_revision!==instance.queue.serverRevision){
          if(instance.queue.confirmed<instance.queue.localRevision||instance.composing)instance.localConflict=true;
          else {instance.reloading=true;instance.doc.replace(global.FocusPatrolAuthoring.upgradeDocument(options.draft.authoring_document));instance.reloading=false;instance.queue.serverRevision=options.draft.draft_revision;}
        }
      }
      const previous = rootOwners.get(root); if(previous && previous !== instance)previous.leave();
      rootOwners.set(root,instance); root.dataset.draftId=options.draft.draft_id; instance.mount(root); return instance;
    },
    get(id) { return instances.get(id); }, leave(id) { instances.get(id)?.leave(); },
  };
})(window);
