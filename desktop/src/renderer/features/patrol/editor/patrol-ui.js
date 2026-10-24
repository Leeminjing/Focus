/* 本文件对外提供 FocusPatrolUI.node/action/kindLabel 的无状态 DOM 辅助函数。
 * 输入为节点类型、样式、纯文本或动作回调；输出为安全文本节点、可访问按钮或 typed 类型名称。
 * 具体工作流为创建原生节点并绑定明确事件，不维护文档或注入来源 HTML。
 * 示例：const button=FocusPatrolUI.action('检查',()=>workbench.preview())。
 */
(function(root){
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const node=(tag,cls="",text=null)=>{const result=document.createElement(tag);result.className=cls;if(text!=null)result.textContent=text;return result;};
  const action=(label,callback,cls="text-button")=>{const result=node("button",cls,label);result.type="button";result.addEventListener("click",callback);return result;};
  const kindLabel=kind=>({message: localize("消息"),authored_instruction: localize("指令消息"),function_call: localize("工具调用"),function_call_output: localize("工具结果"),custom_tool_call: localize("自定义调用"),custom_tool_call_output: localize("自定义结果"),task_contract: localize("任务合同"),agent_collaboration: localize("协作消息"),selected_context: localize("参考内容"),unknown: localize("未知类型")})[kind] || kind;
  root.FocusPatrolUI={node,action,kindLabel};
})(window);
