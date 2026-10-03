/* 本文件对外提供 FocusPatrolUI.node/action/kindLabel 的无状态 DOM 辅助函数。
 * 输入为节点类型、样式、纯文本或动作回调；输出为安全文本节点、可访问按钮或 typed 类型名称。
 * 具体工作流为创建原生节点并绑定明确事件，不维护文档或注入来源 HTML。
 * 示例：const button=FocusPatrolUI.action('检查',()=>workbench.preview())。
 */
(function(root){
  "use strict";
  const node=(tag,cls="",text=null)=>{const result=document.createElement(tag);result.className=cls;if(text!=null)result.textContent=text;return result;};
  const action=(label,callback,cls="text-button")=>{const result=node("button",cls,label);result.type="button";result.addEventListener("click",callback);return result;};
  const kindLabel=kind=>({message:"消息",authored_instruction:"指令消息",function_call:"工具调用",function_call_output:"工具结果",custom_tool_call:"自定义调用",custom_tool_call_output:"自定义结果",task_contract:"任务合同",agent_collaboration:"协作消息",selected_context:"参考内容",unknown:"未知类型"})[kind] || kind;
  root.FocusPatrolUI={node,action,kindLabel};
})(window);
