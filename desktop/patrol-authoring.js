/* 本文件对外提供 validateEntry / validateDocument 的 authoring 结构校验。
 * 输入为解析后的任意 JSON；输出为空错误或含字段路径的错误文本，不裁剪未知字段或限制 Provider role。
 * 工作流只验证编辑身份和已知元字段类型，未知 content/扩展字段保持自由；Worker 与 renderer 共享同一校验。
 * 示例：validateDocument({schema_version:2,entries:[{role:'custom',content:{future:true}}]}) 返回 null。
 */
(function (root) {
  "use strict";
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const error = (path, expected) => `${path} 需要${expected}，未替换有效文档`;
  function validateEntry(value, path = "entry") {
    if (!object(value)) return error(path, "对象");
    if (value.entry_id !== undefined && (typeof value.entry_id !== "string" || !value.entry_id)) return error(`${path}.entry_id`, "非空字符串");
    if (value.role !== undefined && typeof value.role !== "string") return error(`${path}.role`, "字符串");
    for (const key of ["source_ref", "source_hash", "edited_from", "copied_from"])
      if (value[key] != null && typeof value[key] !== "string") return error(`${path}.${key}`, "字符串或 null");
    if (value.reference_only !== undefined && typeof value.reference_only !== "boolean") return error(`${path}.reference_only`, "布尔值");
    return null;
  }
  function validateTransformation(value, path) {
    if (!object(value)) return error(path, "对象");
    if (Object.keys(value).some(key => !["document_hash", "entry_ids", "operation", "parameters"].includes(key))) return error(path, "已知转换字段");
    if (typeof value.document_hash !== "string") return error(`${path}.document_hash`, "字符串");
    if (!Array.isArray(value.entry_ids) || value.entry_ids.some(id => typeof id !== "string")) return error(`${path}.entry_ids`, "字符串数组");
    if (!["as_text", "placeholder", "rename_call"].includes(value.operation)) return error(`${path}.operation`, "显式转换操作");
    if (value.parameters !== undefined && !object(value.parameters)) return error(`${path}.parameters`, "对象");
    return null;
  }
  function validateDocument(value) {
    if (!object(value) || value.schema_version !== 2 || !Array.isArray(value.entries)) return error("document", "schema_version=2 和 entries 数组");
    if (value.instructions !== undefined && typeof value.instructions !== "string") return error("instructions", "字符串");
    for (const key of ["raw_buffer", "raw_error"])
      if (value[key] != null && typeof value[key] !== "string") return error(key, "字符串或 null");
    for (let index = 0; index < value.entries.length; index++) {
      const invalid = validateEntry(value.entries[index], `entries[${index}]`); if (invalid) return invalid;
    }
    if (value.transformations !== undefined) {
      if (!Array.isArray(value.transformations)) return error("transformations", "数组");
      for (let index = 0; index < value.transformations.length; index++) {
        const invalid = validateTransformation(value.transformations[index], `transformations[${index}]`); if (invalid) return invalid;
      }
    }
    return null;
  }
  const api = { validateEntry, validateDocument };
  root.FocusPatrolAuthoring = api; if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
