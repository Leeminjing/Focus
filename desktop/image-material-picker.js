/**
 * 本文件对外提供「图片材料」在桌面渲染层的判定与组装函数，是材料引用约定的前端唯一归属地。
 *
 * 对外提供:
 *   materialRefPattern() — 材料引用标记的正则（与 focus/messages/material_refs.py 保持一致）
 *   formatMaterialRef(index, materialId) — 生成引用标记
 *   materialRefIds(text) — 取出一段文本里引用的全部材料标识
 *   stripMaterialRefs(text) — 移除文本中的引用标记
 *   canMustView(material) — 判定该材料能否被勾选「本轮必须看」
 *   mustViewBlockReason(material) — 不能勾选的原因（可勾选时返回空串）
 *   syncMustView(selectedIds, materials) — 勾选集与最新材料列表对齐，剔除已失效项
 *   buildOutgoing(materials, text) — 组装本轮发送载荷（引用标记 + 必需清单）
 *
 * 输入为材料 payload（含 material_id / relative_path / is_image / size_bytes）与输入框文本。
 * 输出为勾选判定、对齐后的勾选集，以及 {message, mustViewIds} 发送载荷。
 * 具体工作流为：先按「是否图片且内容非空」判定可勾选性，再在发送时把已勾选材料
 * 依次写成引用标记追加到文本尾部，并把标识清单一并交给运行，使像素在请求层按需注入。
 *
 * 示例:
 *   const { message, mustViewIds } = window.imageMaterialPicker.buildOutgoing(materials, "看这张");
 */
(function (global) {
  "use strict";

  const REF_SOURCE = "【图片(\\d+) material_id=([A-Za-z0-9_-]+)】";

  function materialRefPattern() {
    return new RegExp(REF_SOURCE, "g");
  }

  function formatMaterialRef(index, materialId) {
    return `【图片${index} material_id=${materialId}】`;
  }

  function materialRefIds(text) {
    if (typeof text !== "string") return [];
    const ids = [];
    const pattern = materialRefPattern();
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (!ids.includes(match[2])) ids.push(match[2]);
    }
    return ids;
  }

  function stripMaterialRefs(text) {
    if (typeof text !== "string") return "";
    return text.replace(materialRefPattern(), "");
  }

  function mustViewBlockReason(material) {
    if (!material) return "材料不存在";
    if (!material.is_image) return "「本轮必须看」只适用于图片材料";
    if (!material.size_bytes) return "该图片内容为空，无法作为必须查看的材料";
    return "";
  }

  function canMustView(material) {
    return mustViewBlockReason(material) === "";
  }

  function syncMustView(selectedIds, materials) {
    const available = new Map(
      (materials || []).filter(canMustView).map(material => [material.material_id, true])
    );
    return (selectedIds || []).filter(id => available.has(id));
  }

  function buildOutgoing(materials, text) {
    const selected = (materials || []).filter(canMustView);
    const body = String(text == null ? "" : text);
    if (!selected.length) return { message: body, mustViewIds: [] };
    const refs = selected
      .map((material, index) => formatMaterialRef(index + 1, material.material_id))
      .join("");
    return {
      message: `${body}${body ? "\n" : ""}${refs}`,
      mustViewIds: selected.map(material => material.material_id),
    };
  }

  global.imageMaterialPicker = {
    materialRefPattern,
    formatMaterialRef,
    materialRefIds,
    stripMaterialRefs,
    canMustView,
    mustViewBlockReason,
    syncMustView,
    buildOutgoing,
  };
})(window);
