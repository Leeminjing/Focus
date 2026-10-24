/* Settings data pane: persisted retention, explicit cleanup preview and backups. */
(function (root) {
  "use strict";
  function create({ api, host, escapeHtml: esc, english = () => false }) {
    let snapshot = null, draft = null, preview = null, busy = false, message = "", failed = false;
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
    function render() {
      if (!host) return;
      const disabled = busy ? "disabled" : "";
      if (!snapshot) { host.innerHTML = `<p role="status">${esc(message || t("正在读取存储信息…", "Loading storage…"))}</p><button data-storage-action="refresh" ${disabled}>${t("重试", "Retry")}</button>`; return; }
      const field = (key, label, min, max) => `<label class="settings-model-field">${label}<input data-storage-field="${key}" type="number" min="${min}" max="${max}" step="1" value="${esc(String(draft[key]))}" ${disabled}></label>`;
      const rows = snapshot.backups.entries.map(row => `<li><span><strong>${esc(row.id)}</strong><small>${esc(size(row.bytes))} · ${esc(row.created_at ? new Date(row.created_at).toLocaleString(english() ? "en-US" : "zh-CN") : t("时间未知", "Unknown date"))}</small></span><span>${esc(row.protected_reason ? t(...(reasons[row.protected_reason] || [row.protected_reason, row.protected_reason])) : t("可清理", "Eligible"))}</span></li>`).join("");
      host.innerHTML = `<header><strong>${t("长期存储维护", "Storage maintenance")}</strong><button class="text-button" data-storage-action="refresh" ${disabled}>${t("刷新容量", "Refresh")}</button></header>
        <dl class="storage-usage"><div><dt>${t("数据库", "Database")}</dt><dd>${size(snapshot.database_bytes)}</dd></div><div><dt>Journal</dt><dd>${size(snapshot.journal_bytes)} · ${snapshot.journal_events} ${t("条", "events")}</dd></div><div><dt>${t("备份", "Backups")}</dt><dd>${size(snapshot.backups.bytes)}</dd></div></dl>
        <p class="muted tiny">${t("Journal 容量包含在数据库内。删除日志会释放可复用空间，数据库文件未必立即缩小。", "Journal size is included in the database. Deleted rows free reusable space; database files may not shrink immediately.")}</p>
        <div class="settings-model-grid">
          ${field("journal_retention_days", t("Journal 保留天数", "Journal retention days"), 1, 3650)}
          ${field("backup_retention_days", t("备份保留天数", "Backup retention days"), 1, 3650)}
          ${field("backup_keep_count", t("每类至少保留的备份数", "Minimum backups per type"), 1, 100)}
          ${field("backup_interval_hours", t("备份间隔（小时，0 为关闭）", "Backup interval (hours, 0 disables)"), 0, 8760)}
        </div>
        <label><input type="checkbox" data-storage-field="automatic_cleanup" ${draft.automatic_cleanup ? "checked" : ""} ${disabled}>${t("启用周期安全清理", "Enable periodic safe cleanup")}</label>
        <p class="muted tiny">${t("周期任务仅在 Focus 运行时每小时检查一次。保留期和最低备份数同时满足后才清理；恢复点、未消费日志、显式保留和引用证据受保护。", "Maintenance checks hourly while Focus is running. Cleanup requires both age and minimum-count rules. Recovery points, unconsumed events, retention pins and referenced evidence are protected.")}</p>
        <div class="ui-toolbar"><button class="primary" data-storage-action="save" ${disabled}>${t("保存策略", "Save policy")}</button><button class="text-button" data-storage-action="preview" ${disabled}>${t("预览安全清理", "Preview cleanup")}</button><button class="text-button" data-storage-action="backup" ${disabled || (!snapshot.backup_available ? "disabled" : "")}>${t("立即备份数据库", "Back up database now")}</button></div>
        ${!snapshot.backup_available ? `<p class="muted tiny">${t("常规备份仅支持内置本地 PostgreSQL；自定义数据库请使用其备份工具。", "Backups require the bundled local PostgreSQL. Use your database tooling for custom deployments.")}</p>` : ""}
        <p class="muted tiny">${t("常规备份是校验过的 PostgreSQL 归档，不含升级依赖快照和工作区文件。", "Regular backups are verified PostgreSQL archives; dependency snapshots and workspace files are excluded.")}</p>
        ${preview ? `<section class="storage-preview"><p>${t("本次最多清理", "This cleanup removes at most")} ${preview.journal_events} ${t("条 Journal，", "journal events and")} ${preview.backup_ids.length} ${t("个备份，备份空间", "backups, freeing backup space of")} ${size(preview.backup_bytes)}。</p><p class="muted tiny">${t("预览有效期 10 分钟；执行时重新检查保护条件。", "Preview expires after 10 minutes; protections are checked again on execution.")}</p><button class="text-button danger" data-storage-action="cleanup" ${disabled || (!preview.journal_events && !preview.backup_ids.length ? "disabled" : "")}>${t("执行本次清理", "Apply this cleanup")}</button></section>` : ""}
        <p role="status" aria-live="polite" class="${failed ? "danger" : ""}">${esc(message)}</p>
        ${snapshot.last_error ? `<p class="danger">${t("最近周期维护失败：", "Last maintenance error: ")}${esc(snapshot.last_error)}</p>` : ""}
        <p class="muted tiny">${t("备份目录：", "Backup directory: ")}${esc(snapshot.backup_directory || "")}</p>
        <details><summary>${t("备份清单", "Backup inventory")} (${snapshot.backups.entries.length})</summary><ul class="storage-backups">${rows || `<li>${t("暂无备份", "No backups yet")}</li>`}</ul></details>`;
    }
    async function load() {
      snapshot = await api("/desktop/api/settings/storage");
      draft ||= { ...snapshot.policy };
      render();
    }
    async function act(action) {
      if (busy) return;
      busy = true; failed = false;
      message = t("正在处理…", "Working…"); render();
      try {
        if (action === "save") {
          await api("/desktop/api/settings/storage/policy", { method: "PUT", body: JSON.stringify(draft) });
          preview = null; message = t("保留策略已保存", "Retention policy saved");
        } else if (action === "preview") {
          if (Object.keys(draft).some(key => draft[key] !== snapshot.policy[key])) {
            throw new Error(t("请先保存策略，再预览清理", "Save the policy before previewing cleanup"));
          }
          preview = await api("/desktop/api/settings/storage/preview", { method: "POST" });
          message = t("请核对清理预览后执行", "Review the preview before applying cleanup");
        } else if (action === "cleanup") {
          if (!preview) return;
          const result = await api("/desktop/api/settings/storage/cleanup", { method: "POST", body: JSON.stringify({ token: preview.token }) });
          preview = null;
          message = t(`已清理 ${result.journal_events_removed} 条日志、${result.backups_removed.length} 个备份`, `Removed ${result.journal_events_removed} events and ${result.backups_removed.length} backups`);
          if (result.errors?.length) { failed = true; message += `；${result.errors.join("；")}`; }
        } else if (action === "backup") {
          const result = await api("/desktop/api/settings/storage/backup", { method: "POST" });
          message = t(`数据库备份已完成：${result.id}`, `Database backup completed: ${result.id}`);
        } else message = "";
        await load();
      } catch (error) {
        failed = true; message = error.message;
        if (action === "cleanup") preview = null;
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
      preview = null;
      // Keep focus in the input; stale previews cannot remain actionable.
      host.querySelector(".storage-preview")?.remove();
    });
    return { load: () => act("refresh"), render };
  }
  root.FocusStorageSettings = { create };
  if (typeof module !== "undefined") module.exports = { create };
})(typeof window !== "undefined" ? window : globalThis);
