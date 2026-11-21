/* Read inherited per-turn attachment manifests for ordinary and curated conversations. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMessageMaterials = api;
})(globalThis, function () {
  "use strict";
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
    return items.map((item, ordinal) => ({ ...item, ordinal, note: String(item.note || "") }));
  }

  return Object.freeze({ materialBindings });
});
