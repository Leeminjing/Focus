/*
 * 本文件对外提供 DesktopApiError 与 decodeResponse 纯 Fetch 响应解码边界。
 * 输入为已返回的 Response、无凭据的 operation 标签和可选正文上限；输出为成功 JSON/文本/空值，或保留状态、类型、结构化 detail 与有界纯文本摘录的类型化错误。
 * 具体工作流为只消费一次响应正文，按内容类型尝试 JSON，成功响应返回解码值，失败响应构造不含请求 header/secret 的安全错误快照；测试替身仅有 json() 时走兼容读取。
 * 示例：`const payload = await FocusHttpResponse.decodeResponse(response, { operation: "start Agent Loop" })`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusHttpResponse = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const DEFAULT_ERROR_BODY_LIMIT = 4096;

  class DesktopApiError extends Error {
    constructor(message, metadata = {}) {
      super(message);
      this.name = "DesktopApiError";
      this.status = metadata.status ?? 0;
      this.statusText = metadata.statusText || "";
      this.contentType = metadata.contentType || "";
      this.operation = metadata.operation || "desktop request";
      this.payload = metadata.payload;
      this.detail = metadata.detail;
      this.code = metadata.code;
      this.bodyExcerpt = metadata.bodyExcerpt || "";
      this.malformedJson = Boolean(metadata.malformedJson);
    }

    toJSON() {
      return {
        name: this.name,
        message: this.message,
        status: this.status,
        statusText: this.statusText,
        contentType: this.contentType,
        operation: this.operation,
        payload: this.payload,
        detail: this.detail,
        code: this.code,
        bodyExcerpt: this.bodyExcerpt,
        malformedJson: this.malformedJson,
      };
    }
  }

  async function decodeResponse(response, options = {}) {
    const operation = String(options.operation || "desktop request");
    const limit = boundedLimit(options.maxErrorBodyChars);
    const status = Number(response?.status ?? (response?.ok ? 200 : 0));
    const statusText = String(response?.statusText || "");
    const contentType = String(response?.headers?.get?.("content-type") || "");
    const body = await readBody(response, contentType);
    if (response?.ok) {
      if (status === 204 || body.empty) return null;
      if (body.malformedJson) {
        throw buildError({ operation, status, statusText, contentType, body, limit, successful: true });
      }
      return body.parsed ? body.value : body.raw;
    }
    throw buildError({ operation, status, statusText, contentType, body, limit, successful: false });
  }

  async function readBody(response, contentType) {
    if (typeof response?.text === "function") {
      const raw = await response.text();
      const trimmed = raw.trim();
      if (!trimmed) return { raw, value: null, parsed: false, empty: true, malformedJson: false };
      const expectsJson = /(?:^|[/+])json(?:;|$)/i.test(contentType) || /^[\[{]/.test(trimmed);
      if (!expectsJson) return { raw, value: null, parsed: false, empty: false, malformedJson: false };
      try {
        return { raw, value: JSON.parse(raw), parsed: true, empty: false, malformedJson: false };
      } catch {
        return { raw, value: null, parsed: false, empty: false, malformedJson: true };
      }
    }
    if (typeof response?.json === "function") {
      try {
        const value = await response.json();
        return { raw: "", value, parsed: true, empty: value == null, malformedJson: false };
      } catch {
        return { raw: "", value: null, parsed: false, empty: true, malformedJson: true };
      }
    }
    return { raw: "", value: null, parsed: false, empty: true, malformedJson: false };
  }

  function buildError({ operation, status, statusText, contentType, body, limit, successful }) {
    const payload = body.parsed ? body.value : undefined;
    const detail = payload && typeof payload === "object" ? payload.detail : undefined;
    const code = objectValue(detail, "code") || objectValue(payload, "code");
    const excerpt = body.raw ? body.raw.slice(0, limit) : "";
    const causal = detailMessage(detail) || detailMessage(payload) || (!body.malformedJson ? excerpt.trim() : "");
    const httpLabel = status ? `HTTP ${status}${statusText ? ` ${statusText}` : ""}` : "HTTP 请求失败";
    const message = successful
      ? `${operation} 返回了无法解码的成功响应（${httpLabel}）`
      : causal
        ? `${operation} 失败（${httpLabel}）：${causal}`
        : `${operation} 失败（${httpLabel}）`;
    return new DesktopApiError(message, {
      status,
      statusText,
      contentType,
      operation,
      payload,
      detail,
      code,
      bodyExcerpt: excerpt,
      malformedJson: body.malformedJson,
    });
  }

  function detailMessage(value) {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object") return "";
    return String(value.message || value.detail || value.code || "");
  }

  function objectValue(value, key) {
    return value && typeof value === "object" && typeof value[key] === "string" ? value[key] : "";
  }

  function boundedLimit(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(256, Math.min(Math.trunc(parsed), 16384)) : DEFAULT_ERROR_BODY_LIMIT;
  }

  return Object.freeze({ DesktopApiError, decodeResponse, DEFAULT_ERROR_BODY_LIMIT });
});
