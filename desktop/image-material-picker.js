/**
 * 本文件对外提供旧图片消息引用的读取与呈现兼容 API。
 *
 * 输入为历史消息文本；输出为旧图片 material_id、格式化旧引用或移除旧引用后的正文。
 * 具体工作流为只识别既有【图片N material_id=...】协议，供旧 checkpoint 和压缩块继续预览；
 * 新运行选择与发送由 run-material-picker 维护，本模块不再作为运行输入事实来源。
 *
 * 示例：materialRefIds("【图片1 material_id=m1】") 返回 ["m1"]。
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

  global.imageMaterialPicker = {
    materialRefPattern,
    formatMaterialRef,
    materialRefIds,
    stripMaterialRefs,
  };
})(window);
