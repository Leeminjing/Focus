/* Settings data pane: nested capacity, retention policy, database backups and two-step cleanup.
 * Capacity, policy draft, token-bound preview and operation feedback stay independent.
 * Editing policy invalidates the preview without replacing the focused input; cleanup
 * submits only the server token and leaves expiry/protection checks to the backend.
 */
(function (root) {
  "use strict";
  function create({ api, host, escapeHtml: esc, english = () => false }) {
    let snapshot = null, draft = null, preview = null, busy = false;
    const status = { refresh: {}, save: {}, backup: {}, preview: {}, cleanup: {} };
    const t = (zh, en) => english() ? en : zh;
    const size = value => {
      let amount = Number(value || 0), unit = 0;
      while (amount >= 1024 && unit < 4) { amount /= 1024; unit += 1; }
      return `${amount.toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
    };
    const reasons = {
      active_recovery: ["正在恢复", "Active recovery"], recovery_pending: ["恢复尚未完成", "Recovery pending"],
      accepted_recovery_point: ["当前升级回滚点", "Current upgrade recovery point"], recent_backup: ["最近备份", "Recent backup"],
      within_retention: ["保留期内", "Within retention"], unrecognised_or_incomplete: ["未知或未完成，保留", "Unknown or incomplete; retained"],
      linked_or_not_directory: ["链接或异常目录，保留", "Linked or unusual entry; retained"],
    };
    function invalidatePreview(message = "") {
      preview = null;
      status.preview = { message };
    }
    const feedback = action => `<p data-storage-status="${action}" role="status" aria-live="polite" class="storage-status ${status[action].failed ? "danger" : "muted tiny"}">${esc(status[action].message || "")}</p>`;
    function render() {
      if (!host) return;
      const disabled = busy ? "disabled" : "";
      if (!snapshot) { host.innerHTML = `${feedback("refresh")}<button data-storage-action="refresh" ${disabled}>${t("重试", "Retry")}</button>`; return; }
      const field = (key, label, min, max) => `<label class="settings-field-row"><span>${label}</span><input data-storage-field="${key}" type="number" min="${min}" max="${max}" step="1" value="${esc(String(draft[key]))}" ${disabled}></label>`;
      const rows = snapshot.backups.entries.map(row => `<li><span><strong>${esc(row.id)}</strong><small>${esc(size(row.bytes))} · ${esc(row.created_at ? new Date(row.created_at).toLocaleString(english() ? "en-US" : "zh-CN") : t("时间未知", "Unknown date"))}</small></span><span>${esc(row.protected_reason ? t(...(reasons[row.protected_reason] || [row.protected_reason, row.protected_reason])) : t("可清理", "Eligible"))}</span></li>`).join("");
      host.innerHTML = `<header><h3>${t("长期存储维护", "Storage maintenance")}</h3></header>
        <section class="storage-section" aria-labelledby="storageUsageTitle">
        <div class="storage-section-head"><h4 id="storageUsageTitle">${t("存储容量", "Storage usage")}</h4><button class="text-button" data-storage-action="refresh" ${disabled}>${t("刷新容量", "Refresh usage")}</button></div>
        <dl class="storage-usage">
          <div><dt>${t("数据库总量", "Database total")}</dt><dd>${size(snapshot.database_bytes)}</dd>
            <dd class="storage-usage-children"><dl><div><dt>${t("其中 Journal 日志", "Including Journal events")}</dt><dd>${size(snapshot.journal_bytes)} · ${snapshot.journal_events} ${t("条", "events")}</dd></div></dl></dd>
          </div>
          <div><dt>${t("备份文件", "Backup files")}</dt><dd>${size(snapshot.backups.bytes)}</dd></div>
        </dl>
        <p class="muted tiny">${t("删除日志会释放可复用空间，数据库文件未必立即缩小。", "Deleted events free reusable space; database files may not shrink immediately.")}</p>
        ${feedback("refresh")}
        </section>
        <section class="storage-section" aria-labelledby="storagePolicyTitle">
        <h4 id="storagePolicyTitle">${t("保留策略", "Retention policy")}</h4>
        <div class="settings-storage-policy settings-field-list">
          ${field("journal_retention_days", t("Journal 保留天数", "Journal retention days"), 1, 3650)}
          ${field("backup_retention_days", t("备份保留天数", "Backup retention days"), 1, 3650)}
          ${field("backup_keep_count", t("每类至少保留的备份数", "Minimum backups per type"), 1, 100)}
          ${field("backup_interval_hours", t("备份间隔（小时，0 为关闭）", "Backup interval (hours, 0 disables)"), 0, 8760)}
          <label class="settings-field-row"><span>${t("启用周期安全清理", "Enable periodic safe cleanup")}<small>${t("Focus 运行时 · 每小时检查", "While Focus runs · Checked hourly")}</small></span><input type="checkbox" data-storage-field="automatic_cleanup" ${draft.automatic_cleanup ? "checked" : ""} ${disabled}></label>
        </div>
        <details class="storage-protections"><summary>${t("自动维护与保护规则", "Automatic maintenance and protections")}</summary><p class="muted tiny">${t("周期任务仅在 Focus 运行时每小时检查一次。保留期和最低备份数同时满足后才清理；恢复点、未消费日志、显式保留和引用证据受保护。", "Maintenance checks hourly while Focus is running. Cleanup requires both age and minimum-count rules. Recovery points, unconsumed events, retention pins and referenced evidence are protected.")}</p></details>
        <div class="ui-toolbar"><button class="primary" data-storage-action="save" ${disabled}>${t("保存策略", "Save policy")}</button></div>
        ${feedback("save")}
        ${snapshot.last_error ? `<p class="danger">${t("最近周期维护失败：", "Last maintenance error: ")}${esc(snapshot.last_error)}</p>` : ""}
        </section>
        <section class="storage-section" aria-labelledby="storageBackupTitle">
        <h4 id="storageBackupTitle">${t("备份", "Backups")}</h4>
        <div class="storage-backup-scope-group" role="group" aria-labelledby="storageBackupScopeTitle"><h5 id="storageBackupScopeTitle">${t("新建数据库备份范围", "New database backup scope")}</h5><dl class="storage-backup-scope"><div><dt>${t("包含", "Includes")}</dt><dd>${t("校验过的 PostgreSQL 归档", "Verified PostgreSQL archive")}</dd></div><div><dt>${t("不包含", "Excludes")}</dt><dd>${t("工作区文件、升级依赖快照", "Workspace files and upgrade dependency snapshots")}</dd></div></dl></div>
        <div class="ui-toolbar"><button class="text-button" data-storage-action="backup" ${disabled || (!snapshot.backup_available ? "disabled" : "")}>${t("立即备份数据库", "Back up database now")}</button></div>
        ${!snapshot.backup_available ? `<p class="muted tiny">${t("常规备份仅支持内置本地 PostgreSQL；自定义数据库请使用其备份工具。", "Backups require the bundled local PostgreSQL. Use your database tooling for custom deployments.")}</p>` : ""}
        ${feedback("backup")}
        <details class="settings-storage-help"><summary>${t("备份目录", "Backup directory")}</summary><p class="muted tiny">${esc(snapshot.backup_directory || "")}</p></details>
        <details><summary>${t("备份清单", "Backup inventory")} (${snapshot.backups.entries.length})</summary><ul class="storage-backups">${rows || `<li>${t("暂无备份", "No backups yet")}</li>`}</ul></details>
        </section>
        <section class="storage-section" aria-labelledby="storageCleanupTitle">
        <h4 id="storageCleanupTitle">${t("安全清理", "Safe cleanup")}</h4>
        <ol class="storage-cleanup-steps">
          <li><h5>${t("预览清理范围", "Preview cleanup scope")}</h5>
            <button class="text-button" data-storage-action="preview" ${disabled}>${t("预览安全清理", "Preview cleanup")}</button>
            ${preview ? `<section class="storage-preview" aria-labelledby="storagePreviewTitle"><h5 id="storagePreviewTitle">${t("本次最多清理", "Maximum cleanup scope")}</h5><dl class="storage-preview-usage"><div><dt>${t("Journal 日志", "Journal events")}</dt><dd>${preview.journal_events} ${t("条", "events")}</dd></div><div><dt>${t("备份文件", "Backup files")}</dt><dd>${preview.backup_ids.length} ${t("个", "backups")} · ${size(preview.backup_bytes)}</dd></div></dl><p id="storagePreviewExpiry" class="muted tiny">${t("预览有效期 10 分钟；执行时重新检查保护条件。", "Preview expires after 10 minutes; protections are checked again on execution.")}</p></section>` : ""}
            ${feedback("preview")}
          </li>
          <li><h5>${t("核对后执行", "Review and apply")}</h5>
            <p data-storage-execution-hint id="storageExecutionHint" class="muted tiny">${preview ? t("执行范围以本次预览为上限。", "This preview sets the maximum cleanup scope.") : t("请先生成清理预览。", "Generate a cleanup preview first.")}</p>
            <button class="text-button danger" data-storage-action="cleanup" aria-describedby="storageExecutionHint${preview ? " storagePreviewExpiry" : ""}" ${disabled || (!preview || (!preview.journal_events && !preview.backup_ids.length) ? "disabled" : "")}>${t("执行本次清理", "Apply this cleanup")}</button>
            ${feedback("cleanup")}
          </li>
        </ol>
        </section>`;
    }
    async function load() {
      try {
        const next = await api("/desktop/api/settings/storage");
        if (preview && Object.keys(next.policy).some(key => next.policy[key] !== snapshot.policy[key])) {
          invalidatePreview(t("策略已更改，请保存后重新预览。", "Policy changed. Save it and preview again."));
          status.cleanup = {};
        }
        snapshot = next;
        draft ||= { ...snapshot.policy };
        status.refresh = {};
      } catch (error) { status.refresh = { failed: true, message: error.message }; }
    }
    async function act(action) {
      if (busy || (action === "cleanup" && (!preview || (!preview.journal_events && !preview.backup_ids.length)))) return;
      busy = true;
      status[action] = { message: t("正在处理…", "Working…") }; render();
      try {
        if (action === "save") {
          invalidatePreview();
          status.cleanup = {};
          await api("/desktop/api/settings/storage/policy", { method: "PUT", body: JSON.stringify(draft) });
          status.save = { message: t("保留策略已保存", "Retention policy saved") };
          status.preview = { message: t("策略已保存，请重新预览。", "Policy saved. Generate a new preview.") };
          status.cleanup = {};
        } else if (action === "preview") {
          invalidatePreview(t("正在处理…", "Working…"));
          status.cleanup = {};
          if (Object.keys(draft).some(key => draft[key] !== snapshot.policy[key])) {
            throw new Error(t("请先保存策略，再预览清理", "Save the policy before previewing cleanup"));
          }
          preview = await api("/desktop/api/settings/storage/preview", { method: "POST" });
          status.preview = { message: preview.journal_events || preview.backup_ids.length
            ? t("请核对清理预览后执行", "Review the preview before applying cleanup")
            : t("没有可清理项目", "Nothing to clean up") };
        } else if (action === "cleanup") {
          const result = await api("/desktop/api/settings/storage/cleanup", { method: "POST", body: JSON.stringify({ token: preview.token }) });
          invalidatePreview();
          status.cleanup = { message: t(`已清理 ${result.journal_events_removed} 条日志、${result.backups_removed.length} 个备份`, `Removed ${result.journal_events_removed} events and ${result.backups_removed.length} backups`) };
          if (result.errors?.length) { status.cleanup.failed = true; status.cleanup.message += `；${result.errors.join("；")}`; }
        } else if (action === "backup") {
          const result = await api("/desktop/api/settings/storage/backup", { method: "POST" });
          status.backup = { message: t(`数据库备份已完成：${result.id}`, `Database backup completed: ${result.id}`) };
        }
        await load();
      } catch (error) {
        status[action] = { failed: true, message: error.message };
        if (action === "cleanup") invalidatePreview();
      } finally { busy = false; render(); }
    }
    host?.addEventListener("click", event => {
      const button = event.target.closest("[data-storage-action]");
      if (button) void act(button.dataset.storageAction);
    });
    host?.addEventListener("input", event => {
      const key = event.target.dataset.storageField;
      if (!key || !draft) return;
      draft[key] = key === "automatic_cleanup" ? event.target.checked : Number(event.target.value);
      invalidatePreview(t("策略已更改，请保存后重新预览。", "Policy changed. Save it and preview again."));
      status.save = {};
      status.cleanup = {};
      // Keep focus in the input; stale previews cannot remain actionable.
      host.querySelector(".storage-preview")?.remove();
      const execute = host.querySelector('[data-storage-action="cleanup"]');
      if (execute) { execute.disabled = true; execute.setAttribute("aria-describedby", "storageExecutionHint"); }
      const hint = host.querySelector("[data-storage-execution-hint]");
      if (hint) hint.textContent = t("请先生成清理预览。", "Generate a cleanup preview first.");
      for (const action of ["save", "preview", "cleanup"]) {
        const node = host.querySelector(`[data-storage-status="${action}"]`);
        if (node) { node.textContent = status[action].message || ""; node.classList.remove("danger"); }
      }
    });
    return { load: () => act("refresh"), render };
  }
  root.FocusStorageSettings = { create };
  if (typeof module !== "undefined") module.exports = { create };
})(typeof window !== "undefined" ? window : globalThis);
