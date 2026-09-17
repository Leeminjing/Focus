/*
 * 本文件对外提供会话区的 DOM 写入侧：reconcile（对账写入会话内容）、syncStreaming（流式占位同步）、
 * windowLimit/increaseWindow（长会话窗口边界）与 loadEarlier（加载更早内容并保持阅读位置）。输入为
 * 会话容器节点、目标 HTML、流式缓冲与窗口状态；输出为保持节点身份与展开态的 DOM 更新，以及
 * 一次只触碰变化节点的流式写入。具体工作流为 configure 注入对账器、事件归一、渲染模块与 markdown，
 * reconcile 按单元 key/签名对账并复用既有节点，syncStreaming 只重渲染未闭合尾块，loadEarlier 在
 * 提高窗口后按高度差回填滚动位置。对账身份约定：有稳定 key 的单元按 key+签名匹配；无稳定 key 的
 * 顶层节点（占位 article、assembly-empty 等）以内容签名作 key，内容变化即整体替换；事件序列的签名
 * 剔除 <details open>，因为展开态是交互态而非内容。示例：`configure({...}); reconcile(conversation, html)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  let deps = null;
  const windows = new Map();
  const lazyDetailHosts = new WeakSet();

  function configure(next) {
    deps = next;
  }

  function windowLimit(taskId) {
    return windows.get(taskId) || (deps?.render?.WINDOW_MESSAGES ?? 80);
  }

  function increaseWindow(taskId) {
    const step = deps?.render?.WINDOW_MESSAGES ?? 80;
    const next = windowLimit(taskId) + step;
    windows.set(taskId, next);
    return next;
  }

  function loadEarlier(conversation, taskId, reconcileFn) {
    if (!conversation) return;
    const previousHeight = conversation.scrollHeight || 0;
    const previousTop = conversation.scrollTop || 0;
    increaseWindow(taskId);
    if (typeof reconcileFn === "function") reconcileFn();
    conversation.scrollTop = previousTop + Math.max(0, (conversation.scrollHeight || 0) - previousHeight);
  }

  function _unitFromNode(node) {
    if (node.dataset?.streamRun !== undefined) {
      return { kind: "streaming", key: `stream:${node.dataset.streamRun}`, signature: node.outerHTML || "" };
    }
    if (node.dataset?.messageKey !== undefined) {
      return { kind: "message", key: node.dataset.messageKey, signature: node.outerHTML || "" };
    }
    if (node.dataset?.dividerKey !== undefined) {
      return { kind: "divider", key: node.dataset.dividerKey, signature: node.outerHTML || "" };
    }
    if (node.classList?.contains("conversation-event-sequence")) {
      const first = node.querySelector?.(".conversation-event[data-event-key]");
      return first
        ? { kind: "event", key: `seq:${first.dataset.eventKey}`, signature: _eventSequenceSignature(node) }
        : null;
    }
    return { kind: "unkeyed", key: null, signature: node.outerHTML || "" };
  }

  function _eventSequenceSignature(node) {
    return (node.outerHTML || "").replace(/\s+open(="[^"]*")?/g, "");
  }

  function _syncElementAttributes(current, next) {
    for (const attribute of [...current.attributes]) {
      if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of [...next.attributes]) current.setAttribute(attribute.name, attribute.value);
  }

  function _syncEventElement(current, next) {
    if (_eventSequenceSignature(current) === _eventSequenceSignature(next)) return;
    const wasOpen = current.open;
    _syncElementAttributes(current, next);
    current.replaceChildren(...next.childNodes);
    current.open = wasOpen;
  }

  function _transplantStableConversationEvents(currentNodes, nextNodes) {
    const currentByKey = new Map();
    const ambiguous = new Set();
    for (const node of currentNodes) {
      for (const event of node.querySelectorAll?.(".conversation-event[data-event-key]") || []) {
        const key = event.dataset.eventKey;
        if (currentByKey.has(key)) ambiguous.add(key);
        else currentByKey.set(key, event);
      }
    }
    for (const key of ambiguous) currentByKey.delete(key);
    const consumed = new Set();
    for (const node of nextNodes) {
      for (const nextEvent of [...(node.querySelectorAll?.(".conversation-event[data-event-key]") || [])]) {
        const key = nextEvent.dataset.eventKey;
        const currentEvent = currentByKey.get(key);
        if (!currentEvent || consumed.has(key)) continue;
        consumed.add(key);
        _syncEventElement(currentEvent, nextEvent);
        nextEvent.replaceWith(currentEvent);
      }
    }
  }

  function _reconcileEventSequence(current, next) {
    if (!current.matches?.(".conversation-event-sequence") || !next.matches?.(".conversation-event-sequence")) return false;
    const currentEvents = [...current.children];
    const nextEvents = [...next.children];
    if ([...currentEvents, ...nextEvents].some(node => !node.matches?.(".conversation-event[data-event-key]"))) return false;
    const currentByKey = new Map();
    for (const event of currentEvents) {
      if (currentByKey.has(event.dataset.eventKey)) return false;
      currentByKey.set(event.dataset.eventKey, event);
    }
    const nextKeys = new Set();
    const reconciled = [];
    for (const nextEvent of nextEvents) {
      const key = nextEvent.dataset.eventKey;
      if (nextKeys.has(key)) return false;
      nextKeys.add(key);
      const currentEvent = currentByKey.get(key);
      if (!currentEvent) {
        reconciled.push(nextEvent);
        continue;
      }
      _syncEventElement(currentEvent, nextEvent);
      reconciled.push(currentEvent);
    }
    _syncElementAttributes(current, next);
    current.replaceChildren(...reconciled);
    return true;
  }

  function mountLazyDetails(conversation) {
    if (!conversation || typeof conversation.addEventListener !== "function") return;
    if (lazyDetailHosts.has(conversation)) return;
    lazyDetailHosts.add(conversation);
    conversation.addEventListener("toggle", event => {
      const node = event.target;
      if (!node?.matches?.("details.conversation-event") || !node.open) return;
      const body = node.querySelector?.(".conversation-event-detail[data-detail-lazy='1']");
      if (!body) return;
      const record = deps?.eventIndex?.get(node.dataset.eventKey);
      body.innerHTML = record ? deps.events.renderEventDetail(record) : "";
      body.dataset.detailLazy = "0";
    }, true);
  }

  function reconcile(conversation, html) {
    const template = document.createElement?.("template");
    if (!template?.content || !conversation.replaceChildren || !deps?.reconciler) {
      conversation.innerHTML = html;
      mountLazyDetails(conversation);
      return;
    }
    template.innerHTML = html;
    const prevNodes = [...(conversation.children || [])];
    const nextNodes = [...(template.content.children || [])];
    const plan = _planPatches(prevNodes, nextNodes);
    let patches;
    try {
      patches = deps.reconciler.diffUnits(plan.prevUnits, plan.nextUnits);
    } catch (error) {
      conversation.innerHTML = html;
      mountLazyDetails(conversation);
      return;
    }
    _applyPatches(conversation, patches, plan, prevNodes);
    mountLazyDetails(conversation);
  }

  function _planPatches(prevNodes, nextNodes) {
    const prevUnits = [];
    const prevNodeByIndex = [];
    for (const node of prevNodes) {
      const unit = _unitFromNode(node);
      if (unit === null) continue;
      prevUnits.push(unit);
      prevNodeByIndex.push(node);
    }
    _transplantStableConversationEvents(prevNodes, nextNodes);
    const nextUnits = [];
    const nextNodeByIndex = [];
    for (const node of nextNodes) {
      const unit = _unitFromNode(node);
      if (unit === null) continue;
      nextUnits.push(unit);
      nextNodeByIndex.push(node);
    }
    return { prevUnits, prevNodeByIndex, nextUnits, nextNodeByIndex };
  }

  function _applyPatches(conversation, patches, plan, prevNodes) {
    const { prevNodeByIndex, nextNodeByIndex, nextUnits } = plan;
    const openStatesByEventKey = new Map();
    for (const node of prevNodes) {
      for (const evt of node.querySelectorAll?.(".conversation-event[data-event-key]") || []) {
        openStatesByEventKey.set(evt.dataset.eventKey, evt.open);
      }
    }
    const nextToNode = new Array(nextUnits.length).fill(null);
    const keptPrev = new Set();
    for (const patch of patches) {
      if (patch.op === "keep") {
        nextToNode[patch.nextIndex] = prevNodeByIndex[patch.prevIndex];
        keptPrev.add(patch.prevIndex);
      } else if (patch.op === "update") {
        const currentNode = prevNodeByIndex[patch.prevIndex];
        const nextNode = nextNodeByIndex[patch.nextIndex];
        if (_reconcileEventSequence(currentNode, nextNode)) {
          nextToNode[patch.nextIndex] = currentNode;
          keptPrev.add(patch.prevIndex);
          continue;
        }
        for (const evt of nextNode.querySelectorAll?.(".conversation-event[data-event-key]") || []) {
          if (openStatesByEventKey.has(evt.dataset.eventKey)) evt.open = openStatesByEventKey.get(evt.dataset.eventKey);
        }
        nextToNode[patch.nextIndex] = nextNode;
      } else if (patch.op === "append") {
        nextToNode[patch.nextIndex] = nextNodeByIndex[patch.nextIndex];
      }
    }
    for (let j = 0; j < nextToNode.length; j += 1) {
      if (nextToNode[j] == null) nextToNode[j] = nextNodeByIndex[j];
    }
    for (let i = 0; i < prevNodeByIndex.length; i += 1) {
      if (!keptPrev.has(i)) prevNodeByIndex[i].remove();
    }
    let anchor = null;
    for (let j = 0; j < nextToNode.length; j += 1) {
      const node = nextToNode[j];
      if (anchor === null) {
        if (conversation.firstChild !== node) conversation.insertBefore(node, conversation.firstChild);
      } else if (anchor.nextSibling !== node) {
        conversation.insertBefore(node, anchor.nextSibling);
      }
      anchor = node;
    }
  }

  function syncBlocks(container, blocks, rendered) {
    for (let index = 0; index < blocks.length; index += 1) {
      if (rendered[index] === blocks[index]) continue;
      const template = document.createElement("template");
      template.innerHTML = blocks[index];
      const node = template.content.firstElementChild;
      const existing = container.children[index];
      if (existing) existing.replaceWith(node);
      else container.append(node);
    }
    while (container.children.length > blocks.length) {
      container.lastElementChild.remove();
    }
    return blocks.slice();
  }

  function syncStreaming(article, buffer) {
    const header = deps?.render?.STREAMING_HEADER_HTML || "";
    if (!article.firstElementChild) article.insertAdjacentHTML("afterbegin", header);

    const reasoningHtml = buffer.reasoning
      ? `<section class="conversation-event-sequence" role="group" aria-label="执行过程">${deps.events.renderEvent({ type: "reasoning", content: buffer.reasoning }, { previewMode: "latest" })}</section>`
      : "";
    if (buffer.reasoningRendered !== reasoningHtml) {
      const reasoning = article.querySelector(":scope > .conversation-event-sequence");
      if (reasoningHtml) {
        const template = document.createElement("template");
        template.innerHTML = reasoningHtml;
        const node = template.content.firstElementChild;
        if (reasoning) reasoning.replaceWith(node);
        else article.insertBefore(node, article.querySelector(":scope > .message-rich"));
      } else if (reasoning) {
        reasoning.remove();
      }
      buffer.reasoningRendered = reasoningHtml;
    }

    let body = article.querySelector(":scope > .message-rich");
    if (!buffer.text) {
      if (body) body.remove();
      buffer.blocks = [];
      buffer.blockEntries = [];
      return;
    }
    if (!body) {
      body = document.createElement("div");
      body.className = "message-rich";
      article.append(body);
    }
    const blocks = deps.render.streamingBlocks(deps.markdown, buffer).map(entry => entry.html);
    buffer.blocks = syncBlocks(body, blocks, buffer.blocks || []);
  }

  return { configure, reconcile, mountLazyDetails, syncStreaming, syncBlocks, windowLimit, increaseWindow, loadEarlier };
});
