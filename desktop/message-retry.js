/* Historical turn selection and replay content. Inputs are immutable checkpoint messages;
 * outputs exclude the selected question and every later message. Only the compression
 * block containing the target is expanded; earlier blocks keep their existing meaning.
 * The host owns branch creation, material registration, admission recovery and navigation.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMessageRetry = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const clone = value => JSON.parse(JSON.stringify(value));
  const idOf = message => message?.id || message?.message_id;
  const isHuman = message => ["human", "user"].includes(message?.role) && !message.curation_synthetic;
  const protocol = /(?:\n\n)?<focus_run_materials>\n[\s\S]*?\n<\/focus_run_materials>/g;
  const cleanText = text => String(text ?? "").replace(protocol, "");

  function materialBindings(message) {
    // A derived Context retains the serialized per-turn material manifest even
    // though its ancestor's RunMaterialBinding rows belong to a different task.
    const content = typeof message?.content === "string" ? message.content
      : (Array.isArray(message?.content) ? message.content : []).map(block => block?.text || "").join("\n");
    const match = content.match(/<focus_run_materials>\n([\s\S]*?)\n<\/focus_run_materials>/);
    if (!match) return [];
    let items;
    try { items = JSON.parse(match[1]); } catch { throw new Error("原提问的附件记录无法读取"); }
    if (!Array.isArray(items) || items.some(item => !item || typeof item.material_id !== "string" || !item.material_id || typeof item.relative_path !== "string" || !item.relative_path)) throw new Error("原提问的附件记录不完整");
    return items.map((item, ordinal) => ({ ...item, ordinal, must_view_requested: Boolean(item.must_view), note: String(item.note || "") }));
  }

  function selectTurn(messages, messageId) {
    let question = null;
    let selected = null;
    let matches = 0;
    function walk(items, path, depth) {
      if (depth > 8) throw new Error("历史消息嵌套过深");
      for (const [index, message] of (items || []).entries()) {
        const nextPath = [...path, { items, index }];
        if (Array.isArray(message?.compression?.source)) walk(message.compression.source, nextPath, depth + 1);
        else {
          if (isHuman(message)) question = { message, path: nextPath };
          if (idOf(message) === messageId) {
            matches += 1;
            if (!["human", "user", "ai", "assistant"].includes(message.role) || message.curation_synthetic) throw new Error("这条消息不能重新执行");
            selected = question;
          }
        }
      }
    }
    walk(messages, [], 0);
    if (!messageId || matches !== 1) throw new Error("无法唯一定位原消息，请刷新会话后重试");
    if (!selected) throw new Error("未找到这条回答对应的用户提问");
    return clone({ message: selected.message, messages: selected.path.flatMap(step => step.items.slice(0, step.index)) });
  }

  function editableText(message) {
    if (typeof message.content === "string") return cleanText(message.content);
    return (message.content || []).filter(block => ["text", "input_text"].includes(block?.type)).map(block => cleanText(block.text)).filter(Boolean).join("\n");
  }

  function replayContent(message, editedText = null) {
    if (typeof message.content === "string") return editedText === null ? cleanText(message.content) : editedText;
    const blocks = clone(message.content || []);
    if (editedText === null) return blocks.map(block => ["text", "input_text"].includes(block.type) ? { ...block, text: cleanText(block.text) } : block).filter(block => !["text", "input_text"].includes(block.type) || block.text);
    // Keep images and other non-text blocks while replacing only the editable prose.
    return [{ type: "text", text: editedText }, ...blocks.filter(block => !["text", "input_text"].includes(block.type))];
  }

  return { selectTurn, editableText, replayContent, materialBindings };
});
