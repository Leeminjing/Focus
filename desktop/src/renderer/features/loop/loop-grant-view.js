/*
 * 本文件对外提供 Patrol 的共享授权表单与收窄解析。
 * 输入为当前 Loop grant 与表单，输出为授权控件、只读用量及仅收窄的授权请求。
 * 具体工作流为预算固定无上限并保留真实用量；撤销压缩同时剔除对应 capability 与 gate。
 * 示例：FocusLoopGrantView.grantControls(loop)、readNarrowGrant(form, grant)。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopGrantView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const list = values => escape((values || []).join(", "));

  function grantControls(loop) {
    if (!loop.grant || !["running", "paused", "waiting_user"].includes(loop.status)) return "";
    const grant = loop.grant;
    const compressionEnabled = (grant.delegable_gates || []).includes("compression") && Boolean(grant.compression_policy?.version);
    return localize`<details class="loop-grant"><summary>Delegation 授权</summary><form id="agentLoopGrantForm"><label class="loop-delegation-option"><input name="autonomousCompression" type="checkbox"${compressionEnabled ? " checked" : " disabled"}>允许 Patrol 自主压缩当前 Context（可恢复；取消勾选会立即撤销且不能在此处重新扩大）</label><label>Capabilities（逗号分隔，只能删除）<textarea name="capabilities" required>${list(grant.capabilities)}</textarea></label><label>Context scope（逗号分隔，只能删除）<textarea name="contextScope" required>${list(grant.context_scope)}</textarea></label><label>Permission scope（逗号分隔，只能删除）<textarea name="permissionScope">${list(grant.permission_scope)}</textarea></label><label>Delegable gates（逗号分隔，只能删除）<textarea name="delegableGates">${list(grant.delegable_gates)}</textarea></label><label>更早到期时间（ISO，可留空）<input name="expiresAt" value="${escape(grant.expires_at || "")}"></label><button type="submit">收窄授权并重新观察</button></form>${usageSummary(loop)}<button class="text-button danger" type="button" data-action="loop-revoke-grant">撤销 Patrol 授权</button></details>`;
  }

  function usageSummary(loop) {
    const consumption = loop.accounting?.consumption;
    const usage = loop.usage || {};
    const fields = [["rounds", localize("轮次")], ["model_calls", localize("模型调用")], ["input_tokens", localize("输入 Token")], ["output_tokens", localize("输出 Token")], ["retries", localize("重试")]];
    const rows = fields.map(([key, label]) => {
      const actual = consumption?.actual?.[key] ?? usage[key];
      const reserved = consumption?.unreported_reservations?.[key] ?? 0;
      return localize`<li>${escape(label)}：已结算 ${escape(actual ?? localize("待同步"))}${reserved ? localize`，预留／待确认 ${escape(reserved)}` : ""}</li>`;
    }).join("");
    return localize`<section aria-label="执行用量"><p>预算：无上限（固定）</p><ul>${rows}</ul></section>`;
  }

  function readNarrowGrant(form, grant) {
    const values = new FormData(form);
    const enabled = values.get("autonomousCompression") === "on";
    const split = name => String(values.get(name) || "").split(",").map(value => value.trim()).filter(Boolean);
    return {
      command: "narrow",
      capabilities: split("capabilities").filter(value => enabled || value !== "apply_context_compression"),
      context_scope: split("contextScope"),
      permission_scope: split("permissionScope"),
      delegable_gates: split("delegableGates").filter(value => enabled || value !== "compression"),
      compression_policy: enabled ? grant?.compression_policy || null : null,
      expires_at: String(values.get("expiresAt") || "").trim() || null,
    };
  }

  return Object.freeze({ grantControls, readNarrowGrant });
});
