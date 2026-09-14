/*
 * 本文件对外提供准入待决面板的纯逻辑（不触碰 DOM），是「人做决定前必须看到什么、有哪些动作」的唯一归属地。
 *
 * 对外提供:
 *   ACTIONS — 三个动作的稳定标识（仅允许这一次 / 拒绝 / 切换为完全权限）
 *   RISK_NOTICE_KEYS — 启用完全权限前必须说明的两点文案键
 *   isAccessReview(payload) — 中断载荷是否为准入待决
 *   isMainSubject(agentId) — 待决是否来自主执行身份（决定面板落在主流程还是后台待处理区）
 *   targetLines(payload) — 受治理目标按操作类型分行（读 / 写 / 执行命令），供面板逐行呈现
 *   approvalFields(payload) — 面板字段：工具、目标或命令、执行位置、发起角色、当前访问模式
 *   modeLabelKey(mode) — 访问模式的展示文案键
 *   resumeValue(action) — 该动作对应的中断恢复值
 *   widensAccess(action) — 该动作是否要求把后续运行放宽到完全权限
 *
 * 输入: 中断载荷（`{type, tool, reads, writes, command, cwd, agent_role, access_mode}`）与动作标识。
 * 输出: 字段数组、目标分行、文案键与恢复值；不产生任何副作用。
 *
 * 具体工作流:
 *   (1) 字段只呈现判定所依据的事实：规范化真实目标或命令、执行位置、发起角色、当前访问模式，
 *       不从载荷里推断任何未提供的信息（缺失即留空，不填占位）
 *   (2) 受治理目标按操作类型分行：同一个真实路径可能被读、被写或两者兼有，
 *       「这个路径会不会被改写」正是人的判断依据，因此不合并成一张无标注的路径表
 *   (3) 恢复值只回答「这一次」：拒绝给 reject，其余给 approve；运行期的访问模式在运行开始时
 *       已由服务端派生，因此「切换为完全权限」不写入恢复值，而是由调用方另行持久化到后续运行
 *   (4) 主执行身份的待决落在主流程面板，后台执行主体的待决落在后台待处理区，二者互不抢占
 *
 * 示例:
 *   fields = FocusAccessApproval.approvalFields(payload);
 *   resume = { resume: FocusAccessApproval.resumeValue("approve_once") };
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusAccessApproval = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const ACTIONS = Object.freeze(["approve_once", "reject", "switch_full"]);
  const RISK_NOTICE_KEYS = Object.freeze([
    "access.risk_os_permissions",
    "access.risk_other_reviews",
  ]);
  const MODE_LABELS = Object.freeze({
    workspace: "access.mode_workspace",
    full: "access.mode_full",
  });
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
      {
        key: "access_mode",
        labelKey: "access.field_access_mode",
        value: String(value.access_mode || ""),
        valueKey: modeLabelKey(value.access_mode),
      },
    ];
  }

  function modeLabelKey(mode) {
    return MODE_LABELS[String(mode || "")] || null;
  }

  function resumeValue(action) {
    return { decision: action === "reject" ? "reject" : "approve" };
  }

  function widensAccess(action) {
    return action === "switch_full";
  }

  return {
    ACTIONS,
    RISK_NOTICE_KEYS,
    OPERATION_KEYS,
    isAccessReview,
    isMainSubject,
    targetLines,
    approvalFields,
    modeLabelKey,
    resumeValue,
    widensAccess,
  };
});
