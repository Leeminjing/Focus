/* 本文件对外提供 FocusWorkspacePatrolFeedback 的状态与已提交回答投影。
 * 输入为既有 Live 投影、连接和输入记录，或固定 revision 的会话；输出为真实状态和带 Context/revision 来源的回答卡。
 * 工作流为区分受理、执行和完成，按已提交版本查询最终 assistant 正文，忽略工具调用与推理；不把工作区结果猜测归属到某条输入。
 * 示例：const cards = await Feedback.results(api, projection, signal)。本模块不创建 Loop、Run 或任务。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolFeedback = api;
})(globalThis, function () {
  "use strict";
  function status(projection, connection, model) {
    if (["unavailable", "reconnecting", "resyncing"].includes(connection?.status)) return { state: "disconnected", text: "工作状态连接中断 · 草稿已保留" };
    const latest = model?.rows?.[0];
    if (latest?.status === "failed") return { state: "failed", text: "发送未确认 · 可按原身份重试" };
    if (latest?.status === "sending") return { state: "sending", text: "正在发送 · 等待受理回执" };
    const loop = projection?.loop?.state;
    if (latest?.status === "accepted" && (!projection || (latest.accepted_sequence != null && (projection.loop?.updated_sequence || 0) < latest.accepted_sequence))) return { state: "accepted", text: "已受理 · 等待工作状态更新" };
    const labels = { running: "正在工作", waiting_user: "等待你的处理", paused: "工作已暂停", stopped: "工作已停止", failed: "工作遇到问题", completed: "当前工作已完成" };
    if (loop?.status === "waiting_user" && loop.waiting_reason === "awaiting_input") return { state: "idle", text: "准备接收新输入" };
    if (labels[loop?.status]) return { state: loop.status, text: labels[loop.status] };
    return latest?.status === "accepted" ? { state: "accepted", text: "已受理 · 等待工作状态更新" } : { state: "idle", text: "准备好了 · 拖入材料或写下问题" };
  }
  function answer(conversation) {
    for (const item of [...(conversation.messages || [])].reverse()) {
      const message = item.message || {}, role = message.role || message.type;
      if (!["assistant", "ai"].includes(role) || message.tool_calls?.length || message.additional_kwargs?.tool_calls?.length) continue;
      const text = typeof message.content === "string" ? message.content : (Array.isArray(message.content) ? message.content.filter(block => ["text", "output_text"].includes(block.type)).map(block => block.text || "").join("\n") : "");
      if (text.trim()) return { text, message_id: message.id || null };
    }
    return null;
  }
  function revisions(projection) {
    return Object.values(projection?.contexts || {}).filter(entity => entity.state.current_revision_id)
      .sort((a, b) => b.updated_sequence - a.updated_sequence).slice(0, 6);
  }
  function signature(projection) { return JSON.stringify(revisions(projection).map(item => [item.entity_id, item.state.current_revision_id])); }
  async function results(api, projection, signal) {
    return Promise.all(revisions(projection).map(async entity => {
      const contextId = entity.entity_id, revisionId = entity.state.current_revision_id;
      const base = { context_id: contextId, revision_id: revisionId, title: entity.state.title || entity.state.description || "已提交回答" };
      try {
        const conversation = await api.conversation(projection.loop_id, contextId, { revisionId, limit: 30, signal });
        const content = answer(conversation);
        return content ? { ...base, ...content } : null;
      } catch (error) { if (error.name === "AbortError") throw error; return { ...base, error: "回答读取失败，可打开工作台重试" }; }
    })).then(items => items.filter(Boolean));
  }
  return Object.freeze({ status, answer, signature, results });
});
