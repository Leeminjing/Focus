/*
 * 本文件对外提供 FocusContextCuratorPresentation。输入为 Context 策展 Agent/Revision API
 * payload，输出为统一的展示状态、文案、追平判定与 Attempt 行；不读取 DOM、不修改输入。
 */
(function contextCuratorPresentationModule(global) {
  "use strict";

  const PENDING_REVISIONS = new Set([
    "observed", "preparing", "ready", "publishing", "approval_required",
  ]);

  function trackingLabel(control) {
    return ({
      following: "持续跟踪",
      paused: "已暂停",
      stopped: "已停止",
    })[control] || control || "—";
  }

  function isCaughtUp(agent) {
    const checkpoint = agent?.observed_checkpoint_id;
    return agent?.control_state === "following"
      && agent?.health_state === "idle"
      && Boolean(checkpoint)
      && checkpoint === agent?.prepared_checkpoint_id
      && checkpoint === agent?.published_checkpoint_id
      && !PENDING_REVISIONS.has(agent?.latest_revision?.status);
  }

  function presentationStatus(agent) {
    const control = agent?.control_state || "following";
    if (control === "paused" || control === "stopped") return "interrupted";
    if (agent?.health_state === "running") return "running";
    if (agent?.health_state === "preparing") return "pending";
    if (agent?.health_state === "degraded" || agent?.health_state === "blocked") return "error";
    return "ready";
  }

  function stateLabel(agent) {
    const control = agent?.control_state || "following";
    if (control !== "following") return trackingLabel(control);
    if (agent?.health_state === "running") return "正在更新";
    if (agent?.health_state === "preparing") return "正在准备";
    if (agent?.health_state === "degraded") return "处理失败";
    if (agent?.health_state === "blocked") {
      return agent?.latest_revision?.status === "approval_required" ? "等待决断" : "处理阻塞";
    }
    return trackingLabel(control);
  }

  function followingMessage(agent) {
    return isCaughtUp(agent)
      ? "正在等待根 Context 的下一个稳定版本。"
      : "已观察到新版本，正在等待处理。";
  }

  function attemptRows(revision) {
    return (revision?.attempts || []).map(attempt => ({
      attempt_number: attempt.attempt_number,
      model_name: attempt.model_name || "—",
      output_method: attempt.output_method || "—",
      status: attempt.status || "—",
      error_text: attempt.error
        ? `${attempt.error_kind || "error"}: ${attempt.error}`
        : "",
    }));
  }

  function messageSemantics(message) {
    const role = ({ user: "human", assistant: "ai" })[message?.role] || message?.role || "system";
    const kind = ["system", "human", "ai", "tool"].includes(role) ? role : "system";
    const labels = { system: "System", human: "Human", ai: "AI", tool: "Tool" };
    const calls = Array.isArray(message?.tool_calls) ? message.tool_calls : [];
    const associations = calls.map(call => ({
      direction: "调用",
      name: call?.name || "tool",
      tool_call_id: call?.id || "",
    }));
    if (kind === "tool") {
      associations.push({
        direction: "响应",
        name: message?.name || "tool",
        tool_call_id: message?.tool_call_id || "",
      });
    }
    return { kind, roleLabel: labels[kind], associations };
  }

  function revisionPlanItemTypes(revision) {
    const types = [];
    for (const attempt of revision?.attempts || []) {
      for (const item of attempt?.parsed_response?.items || []) {
        if (item?.type && !types.includes(item.type)) types.push(item.type);
      }
    }
    return types;
  }

  global.FocusContextCuratorPresentation = Object.freeze({
    attemptRows,
    followingMessage,
    isCaughtUp,
    messageSemantics,
    presentationStatus,
    revisionPlanItemTypes,
    stateLabel,
    trackingLabel,
  });
})(typeof window === "undefined" ? globalThis : window);
