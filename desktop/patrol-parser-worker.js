/* 本文件对外提供大型文档的格式化及文档、字段和内容块的 Worker JSON 解析。
 * 输入为 generation/request_id、kind 和 raw buffer；输出为同身份的结构结果或路径错误，未知字段原样保留。
 * 工作流为 Worker 解析并共享 authoring shape 校验；renderer 依文档版本或 entry 身份合并，保留最后有效文档。
 * 示例：worker.postMessage({generation:1,kind:'document',raw:'{}'})。
 */
importScripts("./patrol-authoring.js");
self.onmessage = event => {
  const { raw, value:document, ...identity } = event.data;
  try {
    if (identity.kind === "format") {
      self.postMessage({ ...identity, raw:raw != null ? raw : JSON.stringify(document, null, 2) }); return;
    }
    const value = JSON.parse(raw);
    const error = identity.kind === "content" ? null : identity.kind === "fields" ?
      self.FocusPatrolAuthoring.validateEntry(value) : self.FocusPatrolAuthoring.validateDocument(value);
    self.postMessage(error ? { ...identity, error } : { ...identity, value });
  } catch (error) { self.postMessage({ ...identity, error: error.message }); }
};
