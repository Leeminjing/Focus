/**
 * 本文件对外提供逐轮材料草稿的纯函数 API。
 *
 * 输入为材料列表、{bindings, notes} 草稿和材料操作；输出为规范化有序绑定和后端
 * material_inputs 载荷。具体工作流为保留用户首次勾选顺序和每材料
 * 备注缓存，取消选择只移出 binding、不删除备注，再次勾选恢复备注；材料消失时清理全部状态，
 * 函数不访问 DOM 或 API。
 *
 * 示例：buildOutgoing(materials, "比较", setNote(toggleMaterial(empty(), "m1"), "m1", "看三章"))。
 */
(function (global) {
  "use strict";

  function empty() {
    return { bindings: [], notes: {} };
  }

  function normalizeSelection(selection, materials) {
    const current = normalizeShape(selection);
    const byId = new Map((materials || []).map(item => [String(item.material_id), item]));
    const notes = Object.fromEntries(
      Object.entries(current.notes).filter(([id]) => byId.has(id))
    );
    const bindings = current.bindings
      .filter((item, index, all) => byId.has(item.materialId)
        && all.findIndex(candidate => candidate.materialId === item.materialId) === index)
      .map(item => ({ materialId: item.materialId, note: String(notes[item.materialId] ?? item.note ?? "") }));
    return { bindings, notes };
  }

  function toggleMaterial(selection, materialId) {
    const current = normalizeShape(selection);
    const id = String(materialId);
    const selected = current.bindings.some(item => item.materialId === id);
    const bindings = selected
      ? current.bindings.filter(item => item.materialId !== id)
      : [...current.bindings, { materialId: id, note: String(current.notes[id] || "") }];
    return {
      bindings,
      notes: current.notes,
    };
  }

  function setNote(selection, materialId, note) {
    const current = normalizeShape(selection);
    const id = String(materialId);
    const text = String(note ?? "");
    return {
      ...current,
      notes: { ...current.notes, [id]: text },
      bindings: current.bindings.map(item => item.materialId === id ? { ...item, note: text } : item),
    };
  }

  function buildOutgoing(materials, message, selection) {
    const current = normalizeSelection(selection, materials);
    return {
      message: String(message ?? ""),
      materialInputs: current.bindings.map(item => ({ material_id: item.materialId, note: item.note })),
    };
  }

  function normalizeShape(selection) {
    const notes = selection?.notes && typeof selection.notes === "object" ? { ...selection.notes } : {};
    const rawBindings = Array.isArray(selection?.bindings) ? selection.bindings : [];
    return {
      bindings: rawBindings
        .filter(item => item && item.materialId)
        .map(item => ({ materialId: String(item.materialId), note: String(item.note ?? notes[item.materialId] ?? "") })),
      notes,
    };
  }

  global.runMaterialPicker = {
    empty,
    normalizeSelection,
    toggleMaterial,
    setNote,
    buildOutgoing,
  };
})(window);
