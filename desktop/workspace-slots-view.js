/*
 * 本文件对外提供 authoritative/isolated workspace slot、lease 与 adoption 状态视图。
 * 输入为 slot 基线、Run lease、diff、采用和冲突记录；输出为可理解且可操作的隔离执行界面。
 * 具体工作流为按 slot 身份分组执行证据并显示采用边界；示例：`FocusWorkspaceSlotsView.render(slots)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspaceSlotsView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function render(slots = []) {
    const ordered = [...slots].sort((a, b) => (a.kind === "authoritative" ? -1 : b.kind === "authoritative" ? 1 : String(a.slot_id).localeCompare(String(b.slot_id))));
    return `<section class="workspace-slots"><h3>Workspace Slots</h3>${ordered.map(slot => `<article class="workspace-slot is-${escape(slot.kind)}"><header><strong>${slot.kind === "authoritative" ? "权威工作区" : `隔离分支 · ${escape(slot.owner_lane_id || "unassigned")}`}</strong><span>${escape(slot.lifecycle)}</span></header><p>${escape(slot.root_path)}</p><dl><dt>Baseline</dt><dd>${escape(String(slot.base_revision || "local").slice(0, 12))}</dd><dt>Revision</dt><dd>${escape(slot.revision)}</dd><dt>Fingerprint</dt><dd>${escape(String(slot.current_fingerprint || "").slice(0, 12))}</dd><dt>Lease</dt><dd>${escape(slot.lease?.mode || "idle")}</dd><dt>Adoption</dt><dd>${escape(slot.adoption?.status || slot.runs?.at?.(0)?.adoption_state || "not required")}</dd></dl>${(slot.runs || []).slice(0, 3).map(run => `<small>${escape(run.run_id)} · ${escape(run.adoption_state)} · ${escape(run.effect_evidence?.at?.(-1)?.changed ? "changed" : "unchanged")}</small>`).join("")}${slot.adoption?.status === "conflict" ? `<p class="slot-conflict" role="alert">${escape(slot.adoption.conflict?.reason || "采用冲突，需要用户处理")}</p>` : ""}</article>`).join("") || "<p>暂无 workspace slot</p>"}</section>`;
  }
  return Object.freeze({ render });
});
