/* 本文件对外提供 PatrolDocument 文档操作和 DraftSaveQueue。
 * 输入为自由文档、保存回调和版本；输出为字段级编辑、至少 100 次结构撤销及每草稿串行合并保存。
 * 工作流为命中 entry identity 更新字段，基线合并保留较新编辑、同字段冲突返回字段名；结构操作记录逆操作。
 * 未知 JSON 字段名始终写为自有数据属性，不改变 entry 原型；entry epoch 区分删除/恢复与单纯重排。
 * 输入法暂缓发送但仍登记脏版本，确认只推进已提交版本；applyFields(id,base,desired,{pendingFields}) 保护尚未解析的新意图。
 * 示例：doc.setField('e1','content','正文'); queue.changed(); await queue.flush()。
 */
(function (root) {
  "use strict";
  class PatrolDocument {
    constructor(document, changed = () => {}) {
      this.value = document;
      this.byId = new Map(document.entries.map(entry => [entry.entry_id, entry]));
      this.entryEpochs = new Map(document.entries.map(entry => [entry.entry_id, 0]));
      this.changed = changed; this.undoStack = []; this.redoStack = []; this.revision = 0;
    }
    setField(id, field, value) {
      const entry = this.byId.get(id); if (!entry || entry[field] === value) return;
      if (entry.source_ref) entry.edited_from ||= entry.source_ref;
      this._write(entry, field, value); this._changed();
    }
    setInstructions(value) { if(this.value.instructions!==value){this.value.instructions=value;this._changed();} }
    versionOf(id) { return this.entryEpochs.get(id); }
    applyFields(id, base, desired, { pendingFields = [] } = {}) {
      const entry = this.byId.get(id); if (!entry) return { conflicts:["entry_id"], changed:[] };
      const ignored = new Set(["entry_id", "fields_base", "fields_buffer", "fields_error"]);
      const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
      const changes = [...new Set([...Object.keys(base), ...Object.keys(desired)])]
        .filter(key => !ignored.has(key) && !equal(base[key], desired[key]));
      const conflicts = changes.filter(key => pendingFields.includes(key) || !equal(entry[key], base[key]) && !equal(entry[key], desired[key]));
      if (conflicts.length) return { conflicts, changed:[] };
      const before = Object.fromEntries(changes.map(key => [key, { present:Object.hasOwn(entry, key), value:entry[key] }]));
      const previousOrigin = entry.edited_from;
      if (changes.length) this._commit(() => {
        for (const key of changes) { if (Object.hasOwn(desired, key)) this._write(entry, key, desired[key]); else delete entry[key]; }
        if (entry.source_ref) entry.edited_from ||= entry.source_ref;
      }, () => {
        for (const key of changes) { if (before[key].present) this._write(entry, key, before[key].value); else delete entry[key]; }
        if (previousOrigin === undefined) delete entry.edited_from; else entry.edited_from = previousOrigin;
      });
      return { conflicts:[], changed:changes };
    }
    insert(entry, index = this.value.entries.length) {
      this._commit(() => this._insert(entry, index), () => this._remove(entry.entry_id));
    }
    remove(id) {
      const index = this.value.entries.findIndex(e => e.entry_id === id), entry = this.byId.get(id);
      if (entry) this._commit(() => this._remove(id), () => this._insert(entry, index));
    }
    copy(id) {
      const original = this.byId.get(id); if (!original) return;
      const entry = structuredClone(original); entry.entry_id = crypto.randomUUID(); entry.copied_from = id;
      this.insert(entry, this.value.entries.indexOf(original) + 1); return entry;
    }
    move(id, index) {
      const before = this.value.entries.findIndex(e => e.entry_id === id);
      if (before < 0 || before === index) return;
      this._commit(() => this._move(id, index), () => this._move(id, before));
    }
    clear() {
      const before = this.value.entries.slice();
      this._commit(() => this._replace([]), () => this._replace(before));
    }
    replace(value) {
      const before = this.value;
      this._commit(() => { this.value = value; this._replace(value.entries); }, () => { this.value = before; this._replace(before.entries); });
    }
    transform(plan) {
      const previous = this.value.transformations;
      this._commit(() => { this.value.transformations = [...(previous || []), plan]; }, () => { this.value.transformations = previous; });
    }
    append(entries) {
      const ids = new Set(this.byId.keys()), added = entries.filter(entry => !ids.has(entry.entry_id));
      this._commit(() => added.forEach(entry => this._insert(entry, this.value.entries.length)), () => added.forEach(entry => this._remove(entry.entry_id)));
    }
    undo() {
      const op = this.undoStack.pop(); if (!op) return false;
      op.undo(); this.redoStack.push(op); this._changed(); return true;
    }
    redo() {
      const op = this.redoStack.pop(); if (!op) return false;
      op.apply(); this.undoStack.push(op); this._changed(); return true;
    }
    _insert(entry, index) { this.value.entries.splice(index, 0, entry); this.byId.set(entry.entry_id, entry); this.entryEpochs.set(entry.entry_id, (this.entryEpochs.get(entry.entry_id) || 0) + 1); }
    _write(entry, key, value) { Object.defineProperty(entry, key, { value, enumerable:true, configurable:true, writable:true }); }
    _remove(id) { this.value.entries.splice(this.value.entries.findIndex(e => e.entry_id === id), 1); this.byId.delete(id); this.entryEpochs.set(id, (this.entryEpochs.get(id) || 0) + 1); }
    _move(id, index) { const entry = this.byId.get(id), epoch = this.versionOf(id); this._remove(id); this._insert(entry, Math.max(0, Math.min(index, this.value.entries.length))); this.entryEpochs.set(id, epoch); }
    _replace(entries) { this.value.entries = entries.slice(); this.byId = new Map(entries.map(e => [e.entry_id, e])); for (const entry of entries) this.entryEpochs.set(entry.entry_id, (this.entryEpochs.get(entry.entry_id) || 0) + 1); }
    _changed() { this.revision++; this.changed(this.revision); }
    _commit(apply, undo) { apply(); this.undoStack.push({ apply, undo }); if (this.undoStack.length > 150) this.undoStack.shift(); this.redoStack = []; this._changed(); }
  }
  class DraftSaveQueue {
    constructor({ save, snapshot, onStatus = () => {}, revision = 0, delay = 400 }) {
      this.save = save; this.snapshot = snapshot; this.onStatus = onStatus; this.serverRevision = revision;
      this.delay = delay; this.localRevision = 0; this.confirmed = 0; this.running = null; this.timer = null; this.error = null;
    }
    changed(deferred = false) {
      this.localRevision++; this.error = null; this.onStatus("pending"); clearTimeout(this.timer);
      this.timer = deferred ? null : setTimeout(() => this.flush().catch(() => {}), this.delay);
    }
    async flush() {
      clearTimeout(this.timer); this.timer = null;
      if (this.held) return false;
      if (this.running) { await this.running; if (this.confirmed < this.localRevision) return this.flush(); return true; }
      if (this.confirmed === this.localRevision) return true;
      const submitted = this.localRevision, body = this.snapshot(this.serverRevision);
      this.onStatus("saving");
      this.running = this.save(body).then(result => {
        this.serverRevision = result.draft_revision; this.confirmed = submitted; this.error = null;
        this.onStatus(this.confirmed === this.localRevision ? "saved" : "pending", result);
      }).catch(error => { this.error = error; this.onStatus("error", error); throw error; }).finally(() => { this.running = null; });
      await this.running;
      if (this.confirmed < this.localRevision) return this.flush(); return true;
    }
    mutate(action) {
      const next = (this.mutations || Promise.resolve()).catch(() => {}).then(async () => {
        await this.flush();
        this.running = action().finally(() => { this.running = null; });
        return await this.running;
      });
      this.mutations = next; return next;
    }
  }
  const api = { PatrolDocument, DraftSaveQueue };
  root.FocusPatrolDocument = api; if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
