/*
 * 本文件对外提供 Loop Mission 编辑器的 render、read、normalizeMission、buildMission 与 handleAction 函数。
 * 输入为结构化 Mission/兼容旧 Goal、表单 DOM 或可测试的字段值；输出为职责互斥的最终结果、执行边界和完成检查 payload。
 * 具体工作流为渲染可重复边界/检查行，事件委托增删行，提交时规范化字段、验证跨分区重复和证据要求，再生成 API Mission。
 * 示例：`const mission = FocusLoopMissionEditor.read(document.querySelector("#agentLoopStartForm"))`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopMissionEditor = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const EVIDENCE = [
    ["test", "测试结果"],
    ["tool", "工具输出"],
    ["artifact", "交付物 / 文件"],
    ["workspace", "Workspace 变更"],
    ["fact", "已验证事实"],
    ["user", "用户确认"],
  ];
  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const clean = value => String(value ?? "").trim().replace(/\s+/g, " ");
  const unique = values => [...new Set((values || []).map(clean).filter(Boolean))];

  function legacyBoundaries(text) {
    return { in_scope: [], required_invariants: [], prohibited_actions: [], legacy_text: text || null };
  }

  function normalizeMission(value = {}) {
    if (value.outcome) {
      return {
        outcome: value.outcome,
        boundaries: {
          in_scope: value.boundaries?.in_scope || [],
          required_invariants: value.boundaries?.required_invariants || [],
          prohibited_actions: value.boundaries?.prohibited_actions || [],
          legacy_text: value.boundaries?.legacy_text ?? null,
        },
        completion_checks: (value.completion_checks || []).map((item, index) => ({
          check_id: item.check_id || `check-${index + 1}`,
          claim: item.claim || "",
          required: item.required !== false,
          expected_evidence_kinds: item.expected_evidence_kinds || ["fact"],
          user_verification: Boolean(item.user_verification),
        })),
      };
    }
    const goal = value.goal || {};
    return {
      outcome: goal.goal || value.title || "",
      boundaries: legacyBoundaries(goal.task_contract || ""),
      completion_checks: (goal.acceptance_criteria || []).map((item, index) => ({
        check_id: item.criterion_id || `check-${index + 1}`,
        claim: item.text || item.criterion_id || "",
        required: item.required !== false,
        expected_evidence_kinds: item.expected_evidence_kinds || ["fact"],
        user_verification: Boolean(item.user_verification),
      })),
    };
  }

  function boundaryRow(kind, value = "") {
    const placeholder = kind === "prohibited_actions" ? "自然语言限制，或系统动作名（如 create_lane）" : kind === "in_scope" ? "范围说明，或 context:<Context ID>" : "必须始终保持的不变量";
    const label = kind === "prohibited_actions" ? "禁止或超范围动作" : kind === "in_scope" ? "允许触及的范围" : "必须保持的不变量";
    return `<div class="loop-mission-row" data-mission-row><input type="text" data-mission-boundary="${escape(kind)}" value="${escape(value)}" placeholder="${escape(placeholder)}" aria-label="${escape(label)}"><button type="button" class="text-button" data-action="mission-remove-row" aria-label="移除此边界">移除</button></div>`;
  }

  function boundaryGroup(kind, title, help, values) {
    const rows = values?.length ? values : [""];
    return `<section class="loop-mission-boundary" data-boundary-group="${escape(kind)}"><header><div><strong>${escape(title)}</strong><small>${escape(help)}</small></div><button type="button" class="text-button" data-action="mission-add-boundary" data-boundary-kind="${escape(kind)}">添加</button></header><div data-mission-rows>${rows.map(value => boundaryRow(kind, value)).join("")}</div></section>`;
  }

  function checkRow(check = {}, index = 0) {
    const evidence = check.user_verification ? "user" : check.expected_evidence_kinds?.[0] || "test";
    const options = EVIDENCE.map(([value, label]) => `<option value="${value}"${value === evidence ? " selected" : ""}>${label}</option>`).join("");
    return `<div class="loop-mission-check" data-mission-check data-check-id="${escape(check.check_id || `check-${index + 1}`)}"><label><span>可验证声明</span><input type="text" data-check-claim value="${escape(check.claim || "")}" required placeholder="例如：核心回归测试全部通过"></label><label><span>需要的证据</span><select data-check-evidence>${options}</select></label><label class="loop-mission-required"><input type="checkbox" data-check-required${check.required === false ? "" : " checked"}>必需</label><button type="button" class="text-button" data-action="mission-remove-check" aria-label="移除此完成检查">移除</button></div>`;
  }

  function render(value = {}) {
    const mission = normalizeMission(value);
    const checks = mission.completion_checks.length ? mission.completion_checks : [{ check_id: "check-1", required: true, expected_evidence_kinds: ["test"] }];
    const legacy = mission.boundaries.legacy_text ? `<details class="loop-mission-legacy"><summary>旧版执行边界（原文保留）</summary><pre>${escape(mission.boundaries.legacy_text)}</pre></details>` : "";
    return `<section class="loop-mission-editor" data-loop-mission-editor><section class="loop-mission-section"><header><span>1</span><div><h3>最终结果</h3><p>Loop 结束时，什么必须已经成为事实？这里只描述结果，不写限制或验收状态。</p></div></header><textarea data-mission-outcome name="missionOutcome" rows="3" required aria-label="最终结果" placeholder="例如：用户可以在当前 Portfolio Map 中实时观察多个 Context 协同完成任务。">${escape(mission.outcome)}</textarea></section><section class="loop-mission-section"><header><span>2</span><div><h3>执行边界</h3><p>Patrol 和 Context 在执行期间不能越过什么边界？交付物不写在这里。</p></div></header><div class="loop-mission-boundaries">${boundaryGroup("in_scope", "允许触及的范围", "明确本 Loop 可以修改或调查的区域", mission.boundaries.in_scope)}${boundaryGroup("required_invariants", "必须保持", "整个执行过程都不能破坏的不变量", mission.boundaries.required_invariants)}${boundaryGroup("prohibited_actions", "禁止 / 超范围", "无论是否有利于目标都不得执行的动作", mission.boundaries.prohibited_actions)}</div>${legacy}</section><section class="loop-mission-section"><header><span>3</span><div><h3>完成检查</h3><p>用什么可追溯证据证明完成？每一项都有稳定标识和证据类别。</p></div><button type="button" class="text-button" data-action="mission-add-check">添加检查</button></header><div class="loop-mission-checks" data-mission-checks>${checks.map(checkRow).join("")}</div></section></section>`;
  }

  function buildMission(values) {
    const completionChecks = (values.completion_checks || []).map((item, index) => {
      const evidence = item.evidence || "test";
      return {
        check_id: item.check_id || `check-${index + 1}`,
        claim: clean(item.claim),
        required: item.required !== false,
        expected_evidence_kinds: evidence === "user" ? [] : [evidence],
        user_verification: evidence === "user",
      };
    }).filter(item => item.claim);
    const mission = {
      outcome: clean(values.outcome),
      boundaries: {
        in_scope: unique(values.in_scope),
        required_invariants: unique(values.required_invariants),
        prohibited_actions: unique(values.prohibited_actions),
        legacy_text: values.legacy_text || null,
      },
      completion_checks: completionChecks,
    };
    if (!mission.outcome) throw new Error("请填写最终结果");
    if (!mission.completion_checks.length) throw new Error("至少需要一条完成检查");
    const roles = [
      ["最终结果", [mission.outcome]],
      ["执行边界", [...mission.boundaries.in_scope, ...mission.boundaries.required_invariants, ...mission.boundaries.prohibited_actions]],
      ["完成检查", mission.completion_checks.map(item => item.claim)],
    ];
    const locations = new Map();
    for (const [role, entries] of roles) {
      for (const entry of entries) {
        const key = clean(entry).toLocaleLowerCase();
        const previous = locations.get(key);
        if (previous && previous !== role) throw new Error(`相同内容不能同时属于${previous}和${role}`);
        locations.set(key, role);
      }
    }
    return mission;
  }

  function read(form) {
    const group = kind => [...form.querySelectorAll(`[data-mission-boundary="${kind}"]`)].map(input => input.value);
    const legacyText = form.querySelector(".loop-mission-legacy pre")?.textContent || null;
    const checks = [...form.querySelectorAll("[data-mission-check]")].map((row, index) => ({
      check_id: row.dataset.checkId || `check-${index + 1}`,
      claim: row.querySelector("[data-check-claim]")?.value,
      evidence: row.querySelector("[data-check-evidence]")?.value,
      required: Boolean(row.querySelector("[data-check-required]")?.checked),
    }));
    return buildMission({
      outcome: form.querySelector("[data-mission-outcome]")?.value,
      in_scope: group("in_scope"),
      required_invariants: group("required_invariants"),
      prohibited_actions: group("prohibited_actions"),
      legacy_text: legacyText,
      completion_checks: checks,
    });
  }

  function handleAction(button) {
    const action = button?.dataset?.action;
    if (action === "mission-add-boundary") {
      const group = button.closest("[data-boundary-group]");
      group?.querySelector("[data-mission-rows]")?.insertAdjacentHTML("beforeend", boundaryRow(button.dataset.boundaryKind || group.dataset.boundaryGroup));
      return true;
    }
    if (action === "mission-remove-row") {
      const row = button.closest("[data-mission-row]");
      const rows = row?.parentElement?.querySelectorAll("[data-mission-row]") || [];
      if (rows.length > 1) row.remove();
      else row?.querySelector("input")?.setAttribute("value", "");
      if (rows.length === 1 && row?.querySelector("input")) row.querySelector("input").value = "";
      return true;
    }
    if (action === "mission-add-check") {
      const editor = button.closest("[data-loop-mission-editor]");
      const container = editor?.querySelector("[data-mission-checks]");
      const index = container?.querySelectorAll("[data-mission-check]").length || 0;
      container?.insertAdjacentHTML("beforeend", checkRow({ check_id: `check-${index + 1}` }, index));
      return true;
    }
    if (action === "mission-remove-check") {
      const row = button.closest("[data-mission-check]");
      const rows = row?.parentElement?.querySelectorAll("[data-mission-check]") || [];
      if (rows.length > 1) row.remove();
      else if (row) row.querySelector("[data-check-claim]").value = "";
      return true;
    }
    return false;
  }

  return Object.freeze({ render, read, normalizeMission, buildMission, handleAction });
});
