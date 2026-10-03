/* 本文件对外提供 PatrolReviewPanel 的真实检查、诊断与显式变换呈现。
 * 输入为文档/保存/检查 API 端口和稳定视图；输出为有有效期的 plan、预算、字段定位及取消/替换动作。
 * 具体工作流为保存当前版 → 请求真实预览 → 按请求代次与文档版本接纳结果；编辑标记过期，修复必须重新确认当前指纹。
 * 示例：await review.check(); review.invalidate(); review.cancelTransformation(0)。不构造 Provider 请求或伪造工具执行。
 */
(function(root){
  "use strict";
  const {node,action,kindLabel}=root.FocusPatrolUI;
  class PatrolReviewPanel {
    constructor(wb){this.wb=wb;this.state="idle";this.ticket=0;this.plan=null;}
    bind(view){this.view=view;this.panel=view.panel;this._state();}
    _state(){this.view?.reviewState(this.state,this.plan);}
    invalidate(){this.ticket++;this.wb.previewPlan=null;this.state=this.plan || this.state==="checking"?"stale":"idle";this._state();if(this.panel && !this.panel.hidden)this.render();}
    open(){if(!this.panel)return;this.restoreFocus=document.activeElement;this.panel.hidden=false;this.render();}
    close(){this.panel.hidden=true;if(this.restoreFocus?.isConnected && this.panel.contains(document.activeElement))this.restoreFocus.focus({preventScroll:true});}
    async check(show=true){
      if(show)this.open();
      let ticket=null;
      try{
        await this.wb.flush();
        if(!this.wb.active)return null;
        ticket=++this.ticket;const generation=this.wb.generation;
        this.state="checking";this._state();this.render();
        const plan=await this.wb.api(`/desktop/api/drafts/${this.wb.draft.draft_id}/preview`,{method:"POST"});
        if(ticket!==this.ticket || generation!==this.wb.generation || !this.wb.active || !this.wb.root.isConnected)return null;
        this.plan=plan;this.wb.previewPlan=plan;this.state=plan.executable?"valid":"invalid";this.error=null;this._state();this.render();this.wb.options.onPreview?.(plan);return plan;
      }catch(error){if(ticket!=null && ticket!==this.ticket)return null;this.state="error";this.error=error.message;this.wb.previewPlan=null;this._state();this.render();this.wb.options.onStale?.();return null;}
    }
    render(){
      if(!this.panel)return;
      const heading=node("header","patrol-review-heading");heading.append(node("h3","patrol-panel-title","检查与实际请求"),action("关闭",()=>this.close()));
      this.panel.replaceChildren(heading);
      if(this.state==="checking"){this.panel.append(node("p","","正在准备真实模型请求…"));return;}
      if(this.state==="stale")this.panel.append(node("p","patrol-review-stale","文档或配置已变化，以下结果已过期，请重新检查"),action("重新检查",()=>this.check()));
      if(this.error)this.panel.append(node("p","patrol-item-error",this.error),action("重试检查",()=>this.check()));
      const plan=this.plan;
      if(plan){
        this.panel.append(node("p","",plan.budget?`输入 ${plan.budget.input} · 工具 ${plan.budget.tools} · 指令 ${plan.budget.instructions} · 输出预留 ${plan.budget.output_reserve}`:"当前检查没有可用预算"));
        for(const diagnostic of plan.diagnostics || [])this.panel.append(this._diagnostic(diagnostic));
      }
      this._transformations();
      if(plan){const details=node("details"),summary=node("summary","","实际 instructions / input / tools 与角色映射");details.append(summary);
        details.ontoggle=()=>{if(details.open && details.children.length===1)details.append(node("pre","",JSON.stringify({request:plan.request,roles:plan.role_mappings,transformations:plan.transformations},null,2)));};this.panel.append(details);}
      if(!plan && !this.error)this.panel.append(node("p","","检查当前上下文后查看预算、诊断和实际请求"));
    }
    _diagnostic(diagnostic){
      const row=node("div","patrol-diagnostic");row.append(node("p","",diagnostic.message));const ids=diagnostic.entry_ids || [];
      if(ids.some(id=>this.wb.doc.byId.has(id)))row.append(action(diagnostic.field?"定位字段":"定位条目",()=>{
        const id=ids.find(id=>this.wb.doc.byId.has(id));this.wb._locate(id,diagnostic.field);this.view.setPage("target");
      }));else row.append(node("span","patrol-global-diagnostic","全局检查问题"));
      if(diagnostic.code==="raw_parse_error")row.append(action("编辑完整 JSON",()=>this.wb._toggleRaw()));
      if(!ids.length && diagnostic.code==="provider_projection")row.append(action("编辑完整 JSON",()=>this.wb._toggleRaw()));
      if(["target_protocol","window_exceeded"].includes(diagnostic.code) && this.wb.options.onEquipment)row.append(action("调整模型与权限",()=>this.wb.options.onEquipment()));
      const options=diagnostic.options || [];
      for(const [operation,label]of [["as_text","转为参考文本"],["placeholder","生成未完成结果"]])if(options.includes(operation) && ids.length){
        const button=action(`${label} · ${ids.length} 项`,()=>this.transform(diagnostic,operation));button.disabled=this.state==="stale";row.append(button);
      }
      return row;
    }
    _transformations(){
      const plans=this.wb.doc.value.transformations || [];if(!plans.length)return;
      const section=node("section","patrol-transformations");section.append(node("h4","","显式变换"));
      plans.forEach((plan,index)=>{
        const row=node("div","patrol-transformation"),stale=this.plan && plan.document_hash!==this.plan.document_hash;
        const label={as_text:"转为参考文本",placeholder:"补充未完成结果"}[plan.operation] || plan.operation;
        const targets=plan.entry_ids.map(id=>{const index=this.wb.doc.value.entries.findIndex(entry=>entry.entry_id===id);return index<0?"已删除条目":`第 ${index+1} 项 · ${kindLabel(this.wb.doc.value.entries[index].kind)}`;});
        row.append(node("p","",`${label} · ${plan.entry_ids.length} 项${stale || this.state==="stale"?" · 需重新确认":""}`),node("small","",`目标：${targets.join("、")}`),action("取消变换",()=>this.cancelTransformation(index)),action("重新确认并替换",()=>this.replaceTransformation(index)));section.append(row);
      });this.panel.append(section);
    }
    async transform(diagnostic,operation){
      if(!this.wb.previewPlan || !diagnostic.options?.includes(operation))return;
      const ids=diagnostic.entry_ids.filter(id=>this.wb.doc.byId.has(id));if(!ids.length)return;
      const existing=(this.wb.doc.value.transformations || []).findIndex(plan=>plan.entry_ids.some(id=>ids.includes(id)));
      this.wb.doc.transform({document_hash:this.plan.document_hash,entry_ids:ids,operation,parameters:{}},existing<0?null:existing);
      await this.check();
    }
    async cancelTransformation(index){this.wb.doc.cancelTransformation(index);await this.check();}
    async replaceTransformation(index){
      const previous=this.wb.doc.value.transformations?.[index];if(!previous)return;
      const plan=await this.check(false);if(!plan)return;
      if(previous!==this.wb.doc.value.transformations?.[index])return;
      const ids=previous.entry_ids.filter(id=>this.wb.doc.byId.has(id));
      if(!ids.length){this.error="变换目标已不存在，可以取消该变换";this.render();return;}
      this.wb.doc.transform({...previous,entry_ids:ids,document_hash:plan.document_hash},index);await this.check();
    }
    destroy(){this.ticket++;this.panel=null;this.view=null;}
  }
  root.FocusPatrolReview={PatrolReviewPanel};
})(window);
