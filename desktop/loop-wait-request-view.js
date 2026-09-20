/**
 * 本文件对外提供 FocusLoopWaitRequestView 的 WaitRequest 渲染与草稿 store。
 * 输入为类型化等待请求、逐请求 UI 状态和转义函数；输出为绑定 request id 的安全控件 HTML 与隔离草稿。
 * 具体工作流为按 response_mode 选择 text/choice/structured/action 控件，展示来源与作用域，提交状态只禁用本请求，草稿按 request id 持久保存并恢复。
 * 示例：`FocusLoopWaitRequestView.render(request, { pending: false })`。
 */

(function initLoopWaitRequestView(global) {
  const fallbackEscape = value => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

  function createDraftStore() {
    const drafts = new Map();
    const storage = global.localStorage;
    const key = requestId => `focus-loop-wait-draft:${requestId}`;
    return Object.freeze({
      get(requestId) {
        if (drafts.has(requestId)) return drafts.get(requestId);
        try { return storage?.getItem(key(requestId)) || ""; } catch { return ""; }
      },
      set(requestId, value) {
        const draft = String(value ?? "");
        drafts.set(requestId, draft);
        try { storage?.setItem(key(requestId), draft); } catch {}
      },
      delete(requestId) {
        drafts.delete(requestId);
        try { storage?.removeItem(key(requestId)); } catch {}
      },
    });
  }

  function render(request, ui = {}, escape = fallbackEscape) {
    if (!request || !["open", "resolving"].includes(request.status || "open")) return "";
    const id = escape(request.request_id || request.wait_request_id);
    const pending = ui.pending || request.status === "resolving";
    const disabled = pending ? " disabled" : "";
    const contract = request.response_contract || {};
    const draftState = parseDraft(ui.draft);
    const draft = escape(typeof ui.draft === "string" && !ui.draft.startsWith("{") ? ui.draft : draftState.answer || "");
    let control = "";
    if (request.response_mode === "text") {
      control = `<textarea name="answer" rows="3" maxlength="${escape(contract.max_length || 12000)}"${disabled}>${draft}</textarea><button class="primary" type="submit"${disabled}>${pending ? "提交中…" : "回复并继续"}</button>`;
    } else if (["single_choice", "multiple_choice"].includes(request.response_mode)) {
      const type = request.response_mode === "multiple_choice" ? "checkbox" : "radio";
      const selected = new Set(Array.isArray(draftState.choice) ? draftState.choice : [draftState.choice].filter(Boolean));
      control = (contract.choices || contract.options || []).map(choice => `<label><input type="${type}" name="choice" value="${escape(choice.value)}"${selected.has(String(choice.value)) ? " checked" : ""}${disabled}>${escape(choice.label || choice.value)}</label>`).join("") + `<button class="primary" type="submit"${disabled}>确认</button>`;
    } else if (request.response_mode === "structured") {
      control = Object.entries(contract.fields || {}).map(([name, field]) => `<label>${escape(field.label || name)}<input name="${escape(name)}" type="${escape(field.type || "text")}" value="${escape(draftState[name] || "")}"${disabled}></label>`).join("") + `<button class="primary" type="submit"${disabled}>确认</button>`;
    } else if (request.response_mode === "action") {
      control = (contract.actions || []).map(action => {
        const inputs = action.input_schema && typeof action.input_schema === "object"
          ? `<fieldset data-wait-action-input="${escape(action.action)}">${Object.entries(action.input_schema).map(([name, value]) => `<label>${escape(name)}<input name="budget:${escape(name)}" type="number" min="0" value="${escape(draftState[`budget:${name}`] ?? value)}"${disabled}></label>`).join("")}</fieldset>`
          : "";
        return `${inputs}<button type="button" data-wait-action="${escape(action.action)}"${disabled}>${escape(action.label || action.action)}</button>`;
      }).join("");
    } else {
      control = `<p class="loop-wait-unsupported">当前客户端暂不支持响应模式 ${escape(request.response_mode)}</p>`;
    }
    const scope = request.scope && Object.keys(request.scope).length ? escape(JSON.stringify(request.scope)) : "当前 Loop";
    return `<section class="loop-wait-request" data-wait-request-id="${id}" data-response-mode="${escape(request.response_mode)}"><header><span>等待你的决定</span><small>${escape(request.kind || "clarification")}</small></header><p>${escape(request.prompt)}</p><div class="loop-wait-meta"><span>来源：${escape(request.created_by || "Patrol")}</span><span>范围：${scope}</span></div><form data-loop-wait-response>${control}</form>${ui.error ? `<p class="loop-wait-error">${escape(ui.error)}</p>` : ""}</section>`;
  }

  function parseDraft(value) {
    if (!value || typeof value !== "string" || !value.startsWith("{")) return {};
    try { return JSON.parse(value); } catch { return {}; }
  }

  global.FocusLoopWaitRequestView = Object.freeze({ createDraftStore, render });
})(window);
