/*
 * 本文件对外提供「本机资源访问模式」这一概念的纯逻辑与文案接线，是前端唯一拥有访问模式的地方。
 *
 * 对外提供:
 *   MODES — 两档模式及其顺序（workspace / full）
 *   DEFAULT_MODE — 字段缺失或无法识别时的取值（最严）
 *   normalize(value) — 归一取值；未知一律按最严处理
 *   readMode(holder) — 从持有该字段的对象读出模式（任务 ui_state 或装备）
 *   writeMode(holder, mode) — 不可变写入，返回新的持有对象
 *   widens(from, to) — 是否放宽（放宽需要风险确认，收窄不需要）
 *   requestBody(mode) — 主运行请求体里承载模式的字段
 *   labelKey(mode) — 模式展示文案键
 *   labelFallback(mode) — 文案表不可用时的中文兜底（文案归属与文案键放在一起）
 *   RISK_NOTICE_KEYS — 放宽前必须说明的两点文案键
 *   riskNoticeHtml(t, actions) — 风险说明的结构与两个按钮；确认动作由调用方命名
 *
 * 输入: 持有访问模式的普通对象（形如 `{access_mode}`）、模式取值、翻译函数 `t(key, fallback)`、
 *   以及 `{confirm, cancel}` 两个动作名。
 * 输出: 归一后的模式、新的持有对象、请求字段与风险说明 HTML；不产生任何副作用。
 *
 * 具体工作流:
 *   (1) 只有一个默认值：缺失或无法识别都按最严的工作区保护处理，绝不臆测为完全权限
 *   (2) 只有一条写入规则：writeMode 不改原对象，调用方负责把结果落盘到正确的持有者
 *       （主运行的 ui_state / 小兵与派生 Agent 的装备）
 *   (3) 只有一处放宽判据：workspace → full 需要风险确认，full → workspace 直接生效
 *   (4) 风险说明只渲染一处：批准面板的放宽确认与作曲区的模式选择器共用它，文案不复制；
 *       两处「确认之后做什么」并不相同（选择器只切模式，批准面板还要放行当前调用），
 *       因此按钮的动作名由调用方给出，结构仍然共用
 *
 * 示例:
 *   const mode = FocusAccessMode.readMode(detail.ui_state);
 *   const body = FocusAccessMode.requestBody(mode);      // → {access_mode: "workspace"}
 *   detail.ui_state = FocusAccessMode.writeMode(detail.ui_state, "full");
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusAccessMode = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const MODES = Object.freeze(["workspace", "full"]);
  const DEFAULT_MODE = "workspace";
  const MODE_LABELS = Object.freeze({
    workspace: Object.freeze({ key: "access.mode_workspace", fallback: "工作区保护" }),
    full: Object.freeze({ key: "access.mode_full", fallback: "本机完全权限" }),
  });
  const RISK_TITLE = Object.freeze({ key: "access.risk_title", fallback: "启用完全权限前确认" });
  const RISK_NOTICE = Object.freeze([
    Object.freeze({
      key: "access.risk_os_permissions",
      fallback: "它不绕过操作系统自身的权限：这些调用仍受当前用户与文件系统的权限限制。",
    }),
    Object.freeze({
      key: "access.risk_other_reviews",
      fallback: "它也不关闭其他人工审核机制：承诺确认、上下文投影决断与压缩范围确认仍然需要你决定。",
    }),
  ]);
  const RISK_ACTIONS = Object.freeze({
    confirm: Object.freeze({ key: "access.risk_confirm", fallback: "确认切换" }),
    cancel: Object.freeze({ key: "access.risk_cancel", fallback: "取消" }),
  });
  const RISK_NOTICE_KEYS = Object.freeze(RISK_NOTICE.map(item => item.key));

  function normalize(value) {
    return MODES.includes(value) ? value : DEFAULT_MODE;
  }

  function readMode(holder) {
    return normalize(holder && typeof holder === "object" ? holder.access_mode : undefined);
  }

  function writeMode(holder, mode) {
    return { ...(holder || {}), access_mode: normalize(mode) };
  }

  function widens(from, to) {
    return normalize(from) === "workspace" && normalize(to) === "full";
  }

  function requestBody(mode) {
    return { access_mode: normalize(mode) };
  }

  function labelKey(mode) {
    return MODE_LABELS[normalize(mode)].key;
  }

  function labelFallback(mode) {
    return MODE_LABELS[normalize(mode)].fallback;
  }

  function riskNoticeHtml(t, actions) {
    const strong = t(RISK_TITLE.key, RISK_TITLE.fallback);
    const paragraphs = RISK_NOTICE.map(item => `<p>${t(item.key, item.fallback)}</p>`).join("");
    const buttons = actions && actions.confirm && actions.cancel
      ? `<div class="access-mode-risk-actions">
        <button type="button" class="primary" data-action="${actions.confirm}">${t(RISK_ACTIONS.confirm.key, RISK_ACTIONS.confirm.fallback)}</button>
        <button type="button" class="text-button" data-action="${actions.cancel}">${t(RISK_ACTIONS.cancel.key, RISK_ACTIONS.cancel.fallback)}</button>
      </div>`
      : "";
    return `<strong>${strong}</strong>${paragraphs}${buttons}`;
  }

  return {
    MODES,
    DEFAULT_MODE,
    RISK_NOTICE_KEYS,
    normalize,
    readMode,
    writeMode,
    widens,
    requestBody,
    labelKey,
    labelFallback,
    riskNoticeHtml,
  };
});
