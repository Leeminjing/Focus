/*
 * 本文件对外提供 FocusPatrolPresence 会话 Patrol 常驻视图适配。输入为真实 PatrolAgent
 * 列表与任务级位置映射，输出为不改写源数据的待命/真实小兵视图模型和渲染位置；工作流
 * 为零 Agent 时建立无后端身份的待命模型，有 Agent 时映射真实身份并让首个 Agent 只读
 * 继承待命位置。示例：FocusPatrolPresence.compose([], { __standby__: { x: .2, y: .6 } })。
 */
(function patrolPresenceModule(global) {
  "use strict";

  const STANDBY_AVATAR_ID = "__standby__";

  function standbyAvatar() {
    return {
      avatar_id: STANDBY_AVATAR_ID,
      agent_id: null,
      presence: "standby",
      label: "Patrol 小兵",
      status: "ready",
      status_label: "待命",
      message: "尚未布置任务，需要时可以安排我出发。",
      action_label: "布置任务",
      latest_run: null,
    };
  }

  function agentAvatar(agent) {
    return {
      ...agent,
      avatar_id: agent.agent_id,
      agent_id: agent.agent_id,
      presence: "agent",
      label: `小兵 ${agent.agent_id.slice(0, 8)}`,
      status: agent.latest_run?.status || "ready",
      action_label: "查看详情",
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
