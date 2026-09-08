/*
 * 本文件对外提供 FocusPatrolPresence 会话 Patrol 常驻视图适配。输入为真实 PatrolAgent
 * 列表与任务级位置映射，输出为不改写源数据的待命/真实小兵视图模型和渲染位置；工作流
 * 为零 Agent 时建立无后端身份的待命模型，有 Agent 时映射真实身份并让首个 Agent 只读
 * 继承待命位置。示例：FocusPatrolPresence.compose([], { __standby__: { x: .2, y: .6 } })。
 */
(function patrolPresenceModule(global) {
  "use strict";

  const STANDBY_AVATAR_ID = "__standby__";
  const curatorView = global.FocusContextCuratorPresentation;

  function standbyAvatar() {
    return {
      avatar_id: STANDBY_AVATAR_ID,
      agent_id: null,
      presence: "standby",
      label: "Patrol 小兵",
      status: "ready",
      status_label: "待命",
      message: "尚未布置任务，需要时可以安排我出发。",
      actions: [
        { id: "configure", label: "布置任务", tone: "primary" },
        { id: "quick-curate", label: "快捷策展", tone: "secondary" },
      ],
      latest_run: null,
    };
  }

  function agentAvatar(agent) {
    const isCurator = agent.mode === "context_curator";
    const control = agent.control_state || "following";
    const health = agent.health_state || "idle";
    const running = health === "running" || ["pending", "running"].includes(agent.latest_run?.status);
    const curatorPresentation = {
      following: { status: "ready", status_label: "持续跟踪", message: curatorView.followingMessage(agent) },
      paused: { status: "interrupted", status_label: "跟踪已暂停", message: "已保留追踪游标，恢复后会处理最新版本。" },
      stopped: { status: "interrupted", status_label: "跟踪已停止", message: "受管 Context 已保留并转为手工管理。" },
    }[control];
    const healthPresentation = control === "following" ? ({
      preparing: { status: "pending", status_label: "正在准备来源", message: "正在生成存储安全的策展来源视图。" },
      degraded: { status: "error", status_label: "处理失败", message: "本次处理失败，最后成功版本仍然可用。" },
      blocked: { status: "error", status_label: "处理阻塞", message: "需要修复模型配置或完成当前投影决断。" },
    }[health]) : null;
    return {
      ...agent,
      avatar_id: agent.agent_id,
      agent_id: agent.agent_id,
      presence: "agent",
      label: `${isCurator ? "Context 策展 · " : ""}小兵 ${agent.agent_id.slice(0, 8)}`,
      status: isCurator && !running ? healthPresentation?.status || curatorPresentation?.status || "ready" : agent.latest_run?.status || "running",
      status_label: isCurator && running ? "正在更新 Context" : healthPresentation?.status_label || curatorPresentation?.status_label,
      message: isCurator && running ? "正在从最新根 checkpoint 更新受管 Context。" : healthPresentation?.message || curatorPresentation?.message,
      actions: [{ id: "details", label: "查看详情", tone: "primary" }],
    };
  }

  function compose(agents, positions) {
    const sourceAgents = Array.isArray(agents) ? agents : [];
    const sourcePositions = positions && typeof positions === "object" ? positions : {};
    if (sourceAgents.length === 0) {
      return {
        avatars: [standbyAvatar()],
        positions: { ...sourcePositions },
      };
    }
    const avatars = sourceAgents.map(agentAvatar);
    const resolvedPositions = { ...sourcePositions };
    const firstId = avatars[0].avatar_id;
    if (!resolvedPositions[firstId] && resolvedPositions[STANDBY_AVATAR_ID]) {
      resolvedPositions[firstId] = { ...resolvedPositions[STANDBY_AVATAR_ID] };
    }
    return { avatars, positions: resolvedPositions };
  }

  global.FocusPatrolPresence = Object.freeze({
    STANDBY_AVATAR_ID,
    standbyAvatar,
    agentAvatar,
    compose,
  });
})(typeof window === "undefined" ? globalThis : window);
