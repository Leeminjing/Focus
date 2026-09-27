/*
 * 本文件对外提供 FocusLoopWaitRecovery.canResume 与 confirmAndResume。
 * 输入为当前开放的类型化 WaitRequest、Loop identity、显式确认函数和专用恢复 API；输出为是否已提交恢复请求。
 * 具体工作流为只允许带 Mission outcome 证据的缺失目标澄清，旧版无类型及真实缺失输入请求保持原等待；先询问用户确认，再以稳定请求身份调用 resume-with-current-mission，
 * 不经过普通聊天或等待文本回复接口。示例：`await FocusLoopWaitRecovery.confirmAndResume({ request, loopId, confirm, submit })`。
 */
(function initLoopWaitRecovery(global) {
  "use strict";

  function canResume(request) {
    return Boolean(
      request && request.kind === "clarification" && request.response_mode === "text"
      && request.scope?.cause === "missing_goal"
      && request.scope?.evidence_identity?.kind === "mission"
      && request.scope?.evidence_identity?.reference_id === "outcome"
      && (request.status || "open") === "open" && request.request_id && Number.isInteger(request.revision),
    );
  }

  async function confirmAndResume({ request, loopId, confirm, submit, beforeSubmit }) {
    if (!canResume(request) || !loopId) return false;
    if (!confirm("确认当前 Mission 已包含完整目标和完成检查，并沿用它继续执行？这不会发送一条普通聊天消息。")) return false;
    beforeSubmit?.();
    await submit(loopId, request.request_id, {
      confirmation: "resume_with_current_mission",
      request_revision: request.revision,
      idempotency_key: `resume-with-current-mission:${request.request_id}:${request.revision}`,
    });
    return true;
  }

  global.FocusLoopWaitRecovery = Object.freeze({ canResume, confirmAndResume });
  if (typeof module === "object" && module.exports) module.exports = global.FocusLoopWaitRecovery;
})(typeof window === "object" ? window : globalThis);
