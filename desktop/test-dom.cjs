/*
 * 本文件对外提供桌面测试用的最小 DOM：createDocument / createElement / createTextNode / parseHtml，
 * 以及元素的子节点、属性、classList、dataset、选择器查询与增删改语义。
 * 输入为 HTML 字符串或标签名；输出为可被会话写入路径真实操作的节点树——子节点数组、插入/替换/删除与
 * 按选择器查询均有真实效果，且属性值与文本保留源文本，使 outerHTML 能原样回读（对账签名依赖它）。
 * 具体工作流为 扫描分词 → 建树（区分空元素、自闭合与原始文本元素）→ 暴露节点 API；选择器引擎支持
 * 标签/类/id/属性、后代与子代组合以及 `:scope >` 锚定；`rootMarker` 把节点标记为已进入文档，供
 * `isConnected` 判定。示例：
 * `const root = createElement("div"); root.innerHTML = "<p class=\"a\">x</p>"; root.children.length === 1`。
 */
"use strict";

const VOID_ELEMENTS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
const RAW_TEXT_ELEMENTS = new Set(["script", "style", "textarea", "title"]);
const ATTRIBUTE_PATTERN = /([^\s"'=<>/]+)(\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeText(value) {
  return String(value).replace(/[&<>"']/g, char => ESCAPES[char]);
}

function camelToKebab(name) {
  return String(name).replace(/[A-Z]/g, char => `-${char.toLowerCase()}`);
}

function kebabToCamel(name) {
  return String(name).replace(/-([a-z])/g, (_match, char) => char.toUpperCase());
}

function parseAttributes(source) {
  const attributes = [];
  ATTRIBUTE_PATTERN.lastIndex = 0;
  let match = ATTRIBUTE_PATTERN.exec(source);
  while (match) {
    const [, name, assignment, doubleQuoted, singleQuoted, unquoted] = match;
    if (assignment === undefined) attributes.push({ name, value: null, quote: '"' });
    else if (doubleQuoted !== undefined) attributes.push({ name, value: doubleQuoted, quote: '"' });
    else if (singleQuoted !== undefined) attributes.push({ name, value: singleQuoted, quote: "'" });
    else attributes.push({ name, value: unquoted, quote: "" });
    match = ATTRIBUTE_PATTERN.exec(source);
  }
  return attributes;
}

function findTagEnd(source, from) {
  let quote = null;
  for (let index = from; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (char === ">") return index;
  }
  return -1;
}

function parseCompound(source) {
  const compound = { scope: false, tag: null, id: null, classes: [], attributes: [] };
  const pattern = /(:scope)|(\*)|([A-Za-z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g;
  let match = pattern.exec(source);
  while (match) {
    if (match[1]) compound.scope = true;
    else if (match[3]) compound.tag = match[3].toLowerCase();
    else if (match[4]) compound.classes.push(match[4]);
    else if (match[5]) compound.id = match[5];
    else if (match[6]) compound.attributes.push({ name: match[6], value: match[7] ?? match[8] ?? match[9] ?? null });
    match = pattern.exec(source);
  }
  return compound;
}

function parseComplex(source) {
  const tokens = String(source).trim().split(/\s*(>)\s*|\s+/).filter(token => token !== undefined && token !== "");
  const steps = [];
  let combinator = null;
  for (const token of tokens) {
    if (token === ">") { combinator = ">"; continue; }
    steps.push({ combinator: steps.length === 0 ? null : (combinator || " "), compound: parseCompound(token) });
    combinator = null;
  }
  return steps;
}

function parseSelector(selector) {
  return String(selector).split(",").map(part => parseComplex(part.trim())).filter(steps => steps.length);
}

function matchesCompound(node, compound, scopeRoot) {
  if (!node || node.nodeType !== 1) return false;
  if (compound.scope && node !== scopeRoot) return false;
  if (compound.tag && compound.tag !== node.tagName) return false;
  if (compound.id && node.getAttribute("id") !== compound.id) return false;
  if (!compound.classes.every(name => node.classList.contains(name))) return false;
  return compound.attributes.every(attribute => (
    attribute.value === null ? node.hasAttribute(attribute.name) : node.getAttribute(attribute.name) === attribute.value
  ));
}

function matchesComplex(node, steps, scopeRoot) {
  if (!matchesCompound(node, steps[steps.length - 1].compound, scopeRoot)) return false;
  let current = node;
  for (let index = steps.length - 2; index >= 0; index -= 1) {
    const combinator = steps[index + 1].combinator;
    if (combinator === ">") {
      current = current.parentNode;
      if (!matchesCompound(current, steps[index].compound, scopeRoot)) return false;
      continue;
    }
    let ancestor = current.parentNode;
    while (ancestor && !matchesCompound(ancestor, steps[index].compound, scopeRoot)) ancestor = ancestor.parentNode;
    if (!ancestor) return false;
    current = ancestor;
  }
  return true;
}

function descendants(root) {
  const found = [];
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType !== 1) continue;
      found.push(child);
      walk(child);
    }
  };
  walk(root);
  return found;
}

class DomNode {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
    this.listeners = new Map();
  }

  get nextSibling() {
    const siblings = this.parentNode ? this.parentNode.childNodes : [];
    const index = siblings.indexOf(this);
    return index === -1 ? null : siblings[index + 1] || null;
  }

  get previousSibling() {
    const siblings = this.parentNode ? this.parentNode.childNodes : [];
    const index = siblings.indexOf(this);
    return index <= 0 ? null : siblings[index - 1];
  }

  get isConnected() {
    let node = this;
    while (node) {
      if (node.rootMarker) return true;
      node = node.parentNode;
    }
    return false;
  }

  _detach() {
    if (!this.parentNode) return;
    const index = this.parentNode.childNodes.indexOf(this);
    if (index !== -1) this.parentNode.childNodes.splice(index, 1);
    this.parentNode = null;
  }

  _adopt(node) {
    if (node === this) throw new Error("不能把节点插入自身");
    if (node.parentNode) node._detach();
  }

  remove() {
    this._detach();
  }

  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const replacements = nodes.flat().filter(node => node !== this);
    if (!replacements.length) { this._detach(); return; }
    const index = parent.childNodes.indexOf(this);
    this._detach();
    parent.insertBefore(replacements[0], parent.childNodes[index] || null);
    for (let offset = 1; offset < replacements.length; offset += 1) {
      parent.insertBefore(replacements[offset], replacements[offset - 1].nextSibling);
    }
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  removeEventListener(type, handler) {
    const handlers = (this.listeners.get(type) || []).filter(item => item !== handler);
    this.listeners.set(type, handlers);
  }

  dispatchEvent(event) {
    if (!event.target) event.target = this;
    let node = this;
    while (node) {
      for (const handler of node.listeners.get(event.type) || []) handler.call(node, event);
      node = node.parentNode;
    }
    return true;
  }
}

class DomText extends DomNode {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = String(data);
  }

  get textContent() { return this.data; }

  set textContent(value) { this.data = String(value); }

  get outerHTML() { return this.data; }

  cloneNode() { return new DomText(this.data); }
}

class DomElement extends DomNode {
  constructor(tagName) {
    super();
    this.nodeType = 1;
    this.tagName = String(tagName).toLowerCase();
    this.attributeList = [];
    this.datasetProxy = null;
    this.templateContent = null;
  }

  get attributes() { return this.attributeList; }

  get children() { return this.childNodes.filter(node => node.nodeType === 1); }

  get firstChild() { return this.childNodes[0] || null; }

  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }

  get firstElementChild() { return this.children[0] || null; }

  get lastElementChild() { const list = this.children; return list[list.length - 1] || null; }

  get content() {
    if (this.tagName !== "template") return null;
    if (!this.templateContent) this.templateContent = new DomElement("#fragment");
    return this.templateContent;
  }

  set rootMarker(value) { this.connectedRoot = Boolean(value); }

  get rootMarker() { return this.connectedRoot === true; }

  getAttribute(name) {
    const key = String(name).toLowerCase();
    const found = this.attributeList.find(attribute => attribute.name === key);
    return found && found.value !== null ? found.value : null;
  }

  hasAttribute(name) {
    const key = String(name).toLowerCase();
    return this.attributeList.some(attribute => attribute.name === key);
  }

  setAttribute(name, value) {
    const key = String(name).toLowerCase();
    const serialized = value === null || value === undefined ? "" : String(value);
    const found = this.attributeList.find(attribute => attribute.name === key);
    if (found) { found.value = serialized; found.quote = '"'; return; }
    this.attributeList.push({ name: key, value: serialized, quote: '"' });
  }

  removeAttribute(name) {
    const key = String(name).toLowerCase();
    this.attributeList = this.attributeList.filter(attribute => attribute.name !== key);
  }

  get className() { return this.getAttribute("class") || ""; }

  set className(value) { this.setAttribute("class", value); }

  get classList() {
    const owner = this;
    const names = () => owner.className.split(/\s+/).filter(Boolean);
    const write = list => owner.setAttribute("class", list.join(" "));
    return {
      contains: name => names().includes(name),
      add: (...values) => write([...new Set([...names(), ...values])]),
      remove: (...values) => write(names().filter(name => !values.includes(name))),
      toggle: (name, force) => {
        const present = names().includes(name);
        const next = force === undefined ? !present : Boolean(force);
        if (next && !present) write([...names(), name]);
        if (!next && present) write(names().filter(item => item !== name));
        return next;
      },
    };
  }

  get dataset() {
    if (!this.datasetProxy) {
      const owner = this;
      this.datasetProxy = new Proxy({}, {
        get: (_target, property) => (typeof property === "string" ? owner.getAttribute(`data-${camelToKebab(property)}`) ?? undefined : undefined),
        set: (_target, property, value) => { owner.setAttribute(`data-${camelToKebab(property)}`, String(value)); return true; },
        has: (_target, property) => typeof property === "string" && owner.hasAttribute(`data-${camelToKebab(property)}`),
        deleteProperty: (_target, property) => { owner.removeAttribute(`data-${camelToKebab(property)}`); return true; },
        ownKeys: () => owner.attributeList.filter(attribute => attribute.name.startsWith("data-")).map(attribute => kebabToCamel(attribute.name.slice(5))),
        getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
      });
    }
    return this.datasetProxy;
  }

  get open() { return this.hasAttribute("open"); }

  set open(value) { if (value) this.setAttribute("open", ""); else this.removeAttribute("open"); }

  get hidden() { return this.hasAttribute("hidden"); }

  set hidden(value) { if (value) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }

  get disabled() { return this.hasAttribute("disabled"); }

  set disabled(value) { if (value) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }

  get value() { return this.getAttribute("value") ?? ""; }

  set value(next) { this.setAttribute("value", next); }

  get style() {
    const owner = this;
    return {
      get cssText() { return owner.getAttribute("style") || ""; },
      set cssText(text) { owner.setAttribute("style", text); },
      setProperty: (name, value) => { owner.setAttribute("style", `${owner.getAttribute("style") || ""}${name}: ${value};`); },
    };
  }

  set style(_value) {}

  get textContent() {
    return this.childNodes.map(node => node.textContent).join("");
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== "" && value !== null && value !== undefined) this.appendChild(new DomText(escapeText(value)));
  }

  _htmlTarget() { return this.content || this; }

  get innerHTML() {
    return this._htmlTarget().childNodes.map(node => node.outerHTML).join("");
  }

  set innerHTML(value) {
    const target = this._htmlTarget();
    target.childNodes = [];
    for (const node of parseHtml(value)) target.appendChild(node);
  }

  get outerHTML() {
    if (this.tagName === "#fragment") return this.innerHTML;
    const attributes = this.attributeList.map(attribute => {
      if (attribute.value === null) return ` ${attribute.name}`;
      if (attribute.quote === "") return ` ${attribute.name}=${attribute.value}`;
      return ` ${attribute.name}=${attribute.quote}${attribute.value}${attribute.quote}`;
    }).join("");
    const head = `<${this.tagName}${attributes}>`;
    if (VOID_ELEMENTS.has(this.tagName)) return head;
    if (this.tagName === "template") return `${head}${this.content.innerHTML}</template>`;
    return `${head}${this.innerHTML}</${this.tagName}>`;
  }

  insertBefore(node, reference) {
    if (!node || node === reference) return node;
    this._adopt(node);
    const index = reference ? this.childNodes.indexOf(reference) : -1;
    if (index === -1) this.childNodes.push(node);
    else this.childNodes.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }

  appendChild(node) { return this.insertBefore(node, null); }

  append(...nodes) { for (const node of nodes) this.appendChild(node); }

  prepend(...nodes) {
    const reference = this.childNodes[0] || null;
    for (const node of nodes) this.insertBefore(node, reference);
  }

  replaceChildren(...nodes) {
    for (const child of [...this.childNodes]) child._detach();
    this.append(...nodes);
  }

  insertAdjacentHTML(position, html) {
    const parsed = parseHtml(html);
    if (position === "afterbegin") {
      const reference = this.childNodes[0] || null;
      for (const node of parsed) this.insertBefore(node, reference);
      return;
    }
    if (position === "beforeend") { this.append(...parsed); return; }
    const parent = this.parentNode;
    if (!parent) return;
    if (position === "beforebegin") { for (const node of parsed) parent.insertBefore(node, this); return; }
    const reference = this.nextSibling;
    for (const node of parsed) parent.insertBefore(node, reference);
  }

  cloneNode(deep = false) {
    const clone = new DomElement(this.tagName);
    clone.attributeList = this.attributeList.map(attribute => ({ ...attribute }));
    if (deep) for (const child of this.childNodes) clone.appendChild(child.cloneNode(true));
    return clone;
  }

  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    const anchored = groups.some(steps => steps.some(step => step.compound.scope));
    const pool = anchored ? [this, ...descendants(this)] : descendants(this);
    const found = pool.filter(node => groups.some(steps => matchesComplex(node, steps, this)));
    return [...new Set(found)];
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  matches(selector) { return parseSelector(selector).some(steps => matchesComplex(this, steps, this)); }

  closest(selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  focus() {}

  blur() {}

  scrollIntoView() {}

  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

function createElement(tagName) {
  return new DomElement(tagName);
}

function createTextNode(data) {
  return new DomText(data);
}

function parseHtml(html) {
  const root = new DomElement("#fragment");
  const stack = [root];
  const source = String(html ?? "");
  let index = 0;
  while (index < source.length) {
    const open = source.indexOf("<", index);
    if (open === -1) { appendText(stack[stack.length - 1], source.slice(index)); break; }
    if (open > index) appendText(stack[stack.length - 1], source.slice(index, open));
    if (source.startsWith("<!--", open)) {
      const end = source.indexOf("-->", open + 4);
      index = end === -1 ? source.length : end + 3;
      continue;
    }
    if (source.startsWith("<!", open) || source.startsWith("<?", open)) {
      const end = source.indexOf(">", open);
      index = end === -1 ? source.length : end + 1;
      continue;
    }
    const closing = source.startsWith("</", open);
    const nameStart = open + (closing ? 2 : 1);
    const nameMatch = /^[A-Za-z][^\s/>]*/.exec(source.slice(nameStart));
    if (!nameMatch) { appendText(stack[stack.length - 1], "<"); index = open + 1; continue; }
    const tagName = nameMatch[0].toLowerCase();
    const tagEnd = findTagEnd(source, nameStart + nameMatch[0].length);
    if (tagEnd === -1) { appendText(stack[stack.length - 1], source.slice(open)); break; }
    const rawAttributes = source.slice(nameStart + nameMatch[0].length, tagEnd);
    if (closing) {
      for (let depth = stack.length - 1; depth > 0; depth -= 1) {
        if (stack[depth].tagName === tagName) { stack.length = depth; break; }
      }
      index = tagEnd + 1;
      continue;
    }
    const element = createElement(tagName);
    element.attributeList = parseAttributes(rawAttributes);
    stack[stack.length - 1].appendChild(element);
    index = tagEnd + 1;
    if (/\/\s*$/.test(rawAttributes) || VOID_ELEMENTS.has(tagName)) continue;
    if (RAW_TEXT_ELEMENTS.has(tagName)) {
      const closeAt = source.toLowerCase().indexOf(`</${tagName}`, index);
      if (closeAt === -1) { appendText(element, source.slice(index)); break; }
      appendText(element, source.slice(index, closeAt));
      const closeEnd = source.indexOf(">", closeAt);
      index = closeEnd === -1 ? source.length : closeEnd + 1;
      continue;
    }
    stack.push(element);
  }
  return [...root.childNodes];
}

function appendText(parent, data) {
  if (data) parent.appendChild(createTextNode(data));
}

function createDocument() {
  return { createElement, createTextNode, parseHtml };
}

module.exports = { createDocument, createElement, createTextNode, parseHtml };
