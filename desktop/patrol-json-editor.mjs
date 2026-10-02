/* 本文件对外提供 createJsonEditor({label}) 的完整 JSON 文本编辑端口。
 * 输入为可访问名称；输出为 element、value、readOnly、focus、selection 和 refresh 的独立编辑器。
 * 工作流由 CodeMirror 保留全文、撤销与输入法状态，仅渲染可见行；真实文本修改向 element 发出 input。
 * 程序赋值不触发 input，相同全文不重置 history；data-patrol-text-editor 声明原生快捷键所有权，Mod-Shift-Z 跨平台重做。
 * 视图切换复用实例，destroy 释放过期卡片编辑器；解析与 shape 校验由工作台 Worker 负责。
 * 示例：const editor=createJsonEditor({label:'结构 JSON'}); root.append(editor.element); editor.value='{}'。
 */
import {EditorState, Compartment, Annotation, Transaction} from '@codemirror/state';
import {EditorView, keymap} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, redo} from '@codemirror/commands';

const external = Annotation.define();

class JsonEditor {
  constructor({label}) {
    this.element = document.createElement('div');
    this.element.className = 'patrol-raw';
    this.element.dataset.patrolTextEditor = '';
    this._readOnly = new Compartment();
    this._locked = false;
    this._view = new EditorView({parent:this.element, state:EditorState.create({extensions:[
      history(), keymap.of([{key:'Mod-Shift-z',run:redo,preventDefault:true}, ...defaultKeymap, ...historyKeymap]),
      this._readOnly.of(EditorState.readOnly.of(false)),
      EditorView.contentAttributes.of({'aria-label':label, spellcheck:'false'}),
      EditorView.updateListener.of(update => {
        if (update.docChanged && update.transactions.some(transaction => !transaction.annotation(external)))
          this.element.dispatchEvent(new InputEvent('input', {bubbles:true}));
      }),
    ]})});
  }
  get value() { return this._view.state.doc.toString(); }
  set value(value) {
    if (value === this.value) return;
    this._view.dispatch({changes:{from:0, to:this._view.state.doc.length, insert:value}, annotations:[external.of(true), Transaction.addToHistory.of(false)], selection:{anchor:0}});
  }
  get hasFocus() { return this._view.hasFocus; }
  get selectionStart() { return this._view.state.selection.main.from; }
  get selectionEnd() { return this._view.state.selection.main.to; }
  setSelectionRange(from, to) { this._view.dispatch({selection:{anchor:from, head:to}}); }
  focus() { this._view.focus(); }
  refresh() { this._view.requestMeasure(); }
  destroy() { this._view.destroy(); }
  get readOnly() { return this._locked; }
  set readOnly(value) {
    this._locked = value;
    this._view.dispatch({effects:this._readOnly.reconfigure(EditorState.readOnly.of(value))});
  }
}

export function createJsonEditor(options) { return new JsonEditor(options); }
