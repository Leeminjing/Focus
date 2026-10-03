/* 本文件对外提供 PatrolAssemblyView 的左右编排布局与状态呈现。
 * 输入为工作台端口及真实挂载容器；输出为来源/目标容器、固定状态栏、检查抽屉与自适应分隔线。
 * 具体工作流为构建稳定控件 → 原位更新数量/位置/检查状态 → 按编辑区宽度切换两栏/页签；不读取 API 或保存第二份文档。
 * 示例：const view=new PatrolAssemblyView(workbench,root); view.update(); view.destroy()。
 */
(function(root){
  "use strict";
  const {node,action,kindLabel}=root.FocusPatrolUI;
  class PatrolAssemblyView {
    constructor(wb,container){
      this.wb=wb;this.container=container;container.className="patrol-workbench";container.replaceChildren();
      const heading=node("header","patrol-document-heading");heading.append(node("h2","","Patrol 上下文"),node("span","patrol-document-hint","挑选来源，编排模型要看到的内容"));
      this.tabs=node("nav","patrol-assembly-tabs");this.tabs.setAttribute("aria-label","上下文编排视图");
      for(const [page,label]of [["source","来源"],["target","上下文"]])this.tabs.append(action(label,()=>this.setPage(page)));
      this.body=node("div","patrol-assembly");this.source=node("div","patrol-source-container");this.target=node("section","patrol-target-pane");this.target.setAttribute("aria-label","可编辑上下文");
      this.divider=node("div","patrol-divider");this.divider.tabIndex=0;this.divider.setAttribute("role","separator");this.divider.setAttribute("aria-orientation","vertical");this.divider.setAttribute("aria-label","调整来源与上下文宽度");this.divider.setAttribute("aria-valuemin","30");this.divider.setAttribute("aria-valuemax","60");
      this.ratio=wb.sourceRatio || 42;this._setRatio(this.ratio);
      this.divider.onpointerdown=event=>{event.preventDefault();this.divider.setPointerCapture(event.pointerId);this.resizing=true;};
      this.divider.onpointermove=event=>{if(!this.resizing)return;const box=this.body.getBoundingClientRect();this._setRatio((event.clientX-box.left)/box.width*100);};
      this.divider.onpointerup=this.divider.onpointercancel=()=>{this.resizing=false;};
      this.divider.onkeydown=event=>{if(["ArrowLeft","ArrowRight"].includes(event.key)){event.preventDefault();this._setRatio(this.ratio+(event.key==="ArrowLeft"?-2:2));}};
      this._target();this._footer();this.panel=node("aside","patrol-panel");this.panel.hidden=true;this.panel.setAttribute("aria-label","上下文检查结果");
      this.body.append(this.source,this.divider,this.target,this.panel);container.append(heading,this.tabs,this.body,this.footer);
      container.dataset.patrolPage=wb.assemblyPage || "target";
      this.resizeObserver=new ResizeObserver(()=>this._adapt());this.resizeObserver.observe(container);this._adapt();
    }
    _target(){
      const wb=this.wb;this.toolbar=node("header","patrol-target-toolbar");const title=node("div","patrol-pane-heading");this.count=node("span","patrol-pane-note");title.append(node("h3","","上下文"),this.count);
      const add=node("details","patrol-add-menu");add.append(node("summary","patrol-tool-button","＋ 新增"));const choices=node("div","patrol-popover");
      for(const role of ["user","assistant","developer","system"])choices.append(action(`${role} 消息`,()=>{wb._add("message",role);add.open=false;}));
      for(const kind of ["function_call","function_call_output","custom_tool_call","custom_tool_call_output","task_contract","agent_collaboration","unknown"])choices.append(action(kindLabel(kind),()=>{wb._add(kind);add.open=false;}));add.append(choices);
      const more=node("details","patrol-add-menu");more.append(node("summary","patrol-tool-button","···"));more.firstChild.setAttribute("aria-label","上下文更多操作");const menu=node("div","patrol-popover");
      menu.append(action("撤销编排",()=>wb._undo(false)),action("重做编排",()=>wb._undo(true)),action("编辑完整语义 JSON",()=>{more.open=false;wb._toggleRaw();}),
        action("检查来源",()=>wb._refreshSources()),action("载入服务器版本（可撤销）",()=>wb._reloadServer()),action("清空上下文",()=>wb.interactions.run(()=>{wb.doc.clear();wb.renderList();})));more.append(menu);
      this.toolbar.append(title,add,more);this.anchor=action("来源将插入末尾",()=>wb.interactions.setAnchor(null),"patrol-insertion-anchor");this.anchor.setAttribute("aria-label","改为末尾插入来源");
      const behavior=node("details","patrol-behavior");behavior.append(node("summary","","基础行为 · instructions"));this.instructions=node("textarea");this.instructions.setAttribute("aria-label","小兵基础行为");this.instructions.value=wb.doc.value.instructions || "";this.instructions.oninput=()=>wb.doc.setInstructions(this.instructions.value);behavior.append(this.instructions);
      this.list=node("div","patrol-list");this.list.setAttribute("aria-label","Focus 语义上下文");this.raw=wb.rawEditor.element;this.raw.hidden=true;this.rawState=node("p","patrol-raw-state");this.rawState.hidden=true;this.rawState.setAttribute("role","status");
      this.content=node("div","patrol-target-content");this.content.append(this.rawState,this.list,this.raw);this.target.append(this.toolbar,this.anchor,behavior,this.content);
      const roles=node("datalist");roles.id="patrol-roles";for(const role of ["user","assistant","developer","system"]){const option=node("option");option.value=role;roles.append(option);}this.target.append(roles);
    }
    _footer(){
      this.footer=node("footer","patrol-assembly-footer");this.status=action("自动保存",()=>this.wb.queue.flush().catch(()=>{}),"patrol-status");
      this.status.setAttribute("aria-live","polite");
      this.checkState=node("span","patrol-check-state","尚未检查");this.checkState.setAttribute("role","status");this.budget=node("span","patrol-budget");
      this.check=action("检查",()=>this.wb.preview(),"patrol-check-button");this.result=action("查看结果",()=>this.wb.review.open());this.result.hidden=true;
      this.footer.append(this.status,this.checkState,this.budget,this.result,this.check);
    }
    _setRatio(value){
      const width=this.body?.clientWidth || 1000,min=Math.max(30,280/width*100),max=Math.min(60,(width-434)/width*100);
      this.ratio=Math.max(min,Math.min(Math.max(min,max),value));this.wb.sourceRatio=this.ratio;this.container.style.setProperty("--patrol-source-ratio",`${this.ratio}%`);this.divider?.setAttribute("aria-valuenow",String(Math.round(this.ratio)));
    }
    _adapt(){const narrow=this.container.clientWidth<760;this.container.dataset.patrolNarrow=String(narrow);this.tabs.hidden=!narrow;this._setRatio(this.ratio);this.setPage(this.container.dataset.patrolPage);this.wb.viewport?.schedule();this.wb.sourceView?.viewport.schedule();}
    setPage(page){this.container.dataset.patrolPage=page;this.wb.assemblyPage=page;[...this.tabs.children].forEach((button,index)=>button.setAttribute("aria-pressed",String(page===(index?"target":"source"))));}
    update(){
      this.count.textContent=`${this.wb.doc.value.entries.length} 项`;
      const id=this.wb.interactions?.anchor,index=this.wb.doc.value.entries.findIndex(entry=>entry.entry_id===id);
      this.anchor.textContent=id==null?"来源将插入末尾":index<0?"插入位置已不存在 · 点击改为末尾":`来源将插入第 ${index+1} 项前 · 点击改为末尾`;
      this.wb.sourceView?.setAnchor(id,index);
    }
    reviewState(state,plan){
      const labels={idle:"尚未检查",checking:"检查中…",valid:"检查通过",invalid:"需要修复",stale:"已过期 · 重新检查",error:"检查失败"};
      this.checkState.textContent=labels[state];this.container.dataset.previewState=state;this.check.disabled=state==="checking";this.result.hidden=!plan;
      this.budget.textContent=plan?.budget?`${state==="stale"?"过期预算 · ":""}${plan.budget.total.toLocaleString()} / ${plan.budget.context_window?.toLocaleString() || "未知"} tokens`:"";
    }
    destroy(){this.resizeObserver.disconnect();}
  }
  root.FocusPatrolAssembly={PatrolAssemblyView};
})(window);
