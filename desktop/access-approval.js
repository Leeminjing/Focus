/*
 * 本文件对外提供准入待决面板的纯逻辑（不触碰 DOM），是「人做决定前必须看到什么、有哪些动作」的唯一归属地。
 *
 * 对外提供:
 *   ACTIONS — 仅允许本次、拒绝、取消和修改常驻模式的稳定标识
 *   OPERATION_KEYS — 受治理目标的操作类型文案键
 *   isAccessReview(payload) — 中断载荷是否为准入待决
 *   isMainSubject(agentId) — 待决是否来自主执行身份（决定面板落在主流程还是后台待处理区）
 *   targetLines(payload) — 受治理目标按操作类型分行（读 / 写 / 执行命令），供面板逐行呈现
 *   approvalFields(payload) — 面板字段：工具、目标或命令、执行位置、发起角色、当前访问模式
 *     （访问模式字段只回传取值，展示文案由访问模式模块给出，此处不维护第二份映射）
 *   switchedMode(action, payload) — 常驻模式修改目标；不切换时为 null
 *   resumeValue(action, payload) — 携带本次审批标识的中断恢复值
 *
 * 输入: 中断载荷（主体、工具、真实目标、当前与申请模式、理由、请求标识）与动作标识。
 * 输出: 字段数组、目标分行、目标模式与恢复值；不产生任何副作用。
 *
 * 具体工作流:
 *   (1) 字段只呈现判定所依据的事实：规范化真实目标或命令、执行位置、发起角色、当前访问模式，
 *       不从载荷里推断任何未提供的信息（缺失即留空，不填占位）
 *   (2) 受治理目标按操作类型分行：同一个真实路径可能被读、被写或两者兼有，
 *       「这个路径会不会被改写」正是人的判断依据，因此不合并成一张无标注的路径表
 *   (3) 恢复值只回答「这一次」并回显请求标识；常驻模式修改由调用方另行保存，
 *       而是由调用方经访问模式模块落盘到后续运行；是否需要风险确认也由该模块判定
 *   (4) 主执行身份的待决落在主流程面板，后台执行主体的待决落在后台待处理区，二者互不抢占
 *
 * 示例:
 *   fields = FocusAccessApproval.approvalFields(payload, FocusAccessMode.labelKey);
 *   resume = { resume: FocusAccessApproval.resumeValue("approve_once") };
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusAccessApproval = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const ACTIONS = Object.freeze(["approve_once", "reject", "cancel", "switch_full"]);
  const OPERATION_KEYS = Object.freeze({
    read: "access.operation_read",
    write: "access.operation_write",
    command: "access.operation_command",
  });

  function isAccessReview(payload) {
    return Boolean(payload) && typeof payload === "object" && payload.type === "access_review";
  }

  function isMainSubject(agentId) {
    return String(agentId || "").startsWith("main:");
  }

  function listed(value) {
    if (!Array.isArray(value)) return [];
    return value.filter(item => typeof item === "string" && item);
  }

  function targetLines(payload) {
    const command = typeof payload?.command === "string" ? payload.command.trim() : "";
    if (command) {
      return [{ operation: "command", operationKey: OPERATION_KEYS.command, value: command }];
    }
    return [
      ...listed(payload?.reads).map(value => ({
        operation: "read", operationKey: OPERATION_KEYS.read, value,
      })),
      ...listed(payload?.writes).map(value => ({
        operation: "write", operationKey: OPERATION_KEYS.write, value,
      })),
    ];
  }

  function approvalFields(payload) {
    const value = payload && typeof payload === "object" ? payload : {};
    const lines = targetLines(value);
    return [
      { key: "tool", labelKey: "access.field_tool", value: String(value.tool || "") },
      {
        key: "target",
        labelKey: "access.field_target",
        value: lines.map(line => line.value).join("\n"),
        lines,
      },
      { key: "cwd", labelKey: "access.field_cwd", value: String(value.cwd || "") },
      { key: "agent_role", labelKey: "access.field_agent_role", value: String(value.agent_role || "") },
      { key: "agent_id", labelKey: "access.field_agent_id", value: String(value.agent_id || "") },
      { key: "run_id", labelKey: "access.field_run_id", value: String(value.run_id || "") },
      {
        key: "access_mode",
        labelKey: "access.field_access_mode",
        value: String(value.access_mode || ""),
        mode: value.access_mode || null,
      },
      { key: "requested_mode", labelKey: "access.field_requested_mode", value: String(value.requested_mode || ""), mode: value.requested_mode || null },
      { key: "reason", labelKey: "access.field_reason", value: String(value.reason || "") },
    ].filter(field => !["agent_id", "run_id", "requested_mode", "reason"].includes(field.key) || field.value);
  }

  function resumeValue(action, payload) {
    const decision = action === "reject" ? "reject" : action === "cancel" ? "cancel" : "approve";
    return {
      decision,
      ...(payload?.request_id ? { request_id: payload.request_id } : {}),
    };
  }

  function switchedMode(action, payload) {
    return action === "switch_full" ? payload?.requested_mode || "danger-full-access" : null;
  }

  return {
    ACTIONS,
    OPERATION_KEYS,
    isAccessReview,
    isMainSubject,
    targetLines,
    approvalFields,
    switchedMode,
    resumeValue,
  };
});
