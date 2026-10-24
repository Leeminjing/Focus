/*
 * 本文件对外提供「作曲区未发送内容」的按任务归属的镜像，是前端唯一持有未发送内容的地方。
 *
 * 对外提供:
 *   claim(taskId, value) — 记录某任务的未发送内容并返回记录值（输入事件即调用）
 *   value(taskId, fallback) — 读某任务的未发送内容；该任务没有镜像时返回 fallback
 *   release(taskId) — 释放某任务的镜像（内容已发送、已命令化或已由持久化取值接管）
 *   size() — 当前持有镜像的任务数（供测试与诊断）
 *
 * 输入: 任务身份（task_id）与作曲区文本；value 的 fallback 由调用方给出（通常是该任务已持久化的
 *   `ui_state.input`）。输出: 记录后的文本、读取到的文本或调用方给的 fallback；不产生任何副作用。
 *
 * 具体工作流:
 *   (1) 未发送内容的唯一事实来源在这里，而不是活动 DOM：输入事件先 claim 再（由调用方）去抖落盘，
 *       因此任何界面重建都只消费本模块的值，不需要每个渲染调用点各自记得先捕获
 *   (2) 归属按传入的 task_id 落位，不按"当前活动任务"推断，避免切换任务时把上一个任务的内容写进新任务
 *   (3) 落盘与本模块无关：持久化时机（去抖、页面隐藏、卸载兜底）由调用方决定；落盘失败不清镜像
 *   (4) 镜像只保存尚未发送的文本：发送成功或文本被命令化后由调用方 release
 *
 * 示例:
 *   FocusComposerDraft.claim("task-a", "还没发的一句话");   // → "还没发的一句话"
 *   FocusComposerDraft.value("task-a", detail.ui_state.input); // 渲染作曲区时读取
 *   FocusComposerDraft.release("task-a");                   // 发送成功后
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusComposerDraft = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const drafts = new Map();

  function claim(taskId, value) {
    if (!taskId) return "";
    const text = String(value ?? "");
    drafts.set(taskId, text);
    return text;
  }

  function value(taskId, fallback = "") {
    return taskId && drafts.has(taskId) ? drafts.get(taskId) : fallback;
  }

  function release(taskId) {
    if (taskId) drafts.delete(taskId);
  }

  function size() {
    return drafts.size;
  }

  return { claim, value, release, size };
});
