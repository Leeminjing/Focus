/** 普通会话交付状态和队列卡片。输入耐久回执，输出可访问的正文与撤回操作。 */
(function initConversationInputs(global) {
  const labels = Object.freeze({ accepted: "已受理，等待当前轮继续", delivered: "已进入当前轮上下文",
    queued: "待发送 · 当前轮结束后执行", started: "已启动下一轮", not_delivered: "本轮结束前未送达",
    blocked: "队列已暂停", withdrawn: "已撤回" });
  function render(rows, escape) {
    return rows.filter(row => row.status !== "withdrawn").map(row => {
      const editable = row.mode === "queue" && ["queued", "blocked"].includes(row.status);
      const text = typeof row.run.message === "string" ? row.run.message : JSON.stringify(row.run.message);
      return `<article class="conversation-input"><strong>${escape(labels[row.status] || row.status)}</strong><p>${escape(text)}</p>${row.error ? `<p>${escape(row.error)}</p>` : ""}${editable ? `<div><button type="button" data-action="edit-queued-main" data-input-id="${escape(row.input_id)}">撤回到输入框编辑</button><button type="button" data-action="delete-queued-main" data-input-id="${escape(row.input_id)}">删除</button></div>` : ""}</article>`;
    }).join("");
  }
  global.FocusConversationInputs = Object.freeze({ labels, render });
})(window);
