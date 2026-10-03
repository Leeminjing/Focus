/* 本文件对外提供 PatrolSourceBrowser 的左侧只读来源视图。
 * 输入为来源控制器、插入/拖动意图回调及挂载容器；输出为紧凑筛选、独立滚动、窗口化来源行和可恢复加载状态。
 * 具体工作流为持久控件消费控制器状态 → 通过控制器切换/取消来源 → 查看/选择精确行 → 回传插入意图；切换清理查询计时器，销毁释放编辑器、观察器和通知。
 * 示例：const view=new PatrolSourceBrowser(controller,{onInsert,onDrag}); container.append(view.node)。
 */
(function(root){
  "use strict";
  const {node,action,kindLabel}=root.FocusPatrolUI;
  class SourceRow {
    constructor(view,row){
      this.view=view;this.row=row;this.node=node("article","patrol-source-row");this.node.dataset.sourceRowId=row.source_row_id;
      const header=node("div","patrol-source-row-heading");
      this.checkbox=node("input");this.checkbox.type="checkbox";this.checkbox.setAttribute("aria-label",`选择${kindLabel(row.kind)}`);
      this.checkbox.onchange=()=>view.controller.select(row.source_row_id,this.checkbox.checked);
      const drag=action("⠿",()=>{},"patrol-source-drag");drag.setAttribute("aria-label","拖动选中来源插入");drag.onpointerdown=event=>{
        if(!row.eligible)return;if(!view.controller.selected.has(row.source_row_id))view.controller.select(row.source_row_id,true);
        view.options.onDrag(event);
      };
      header.append(this.checkbox,node("span","patrol-kind",kindLabel(row.kind)));if(row.role)header.append(node("span","patrol-source-kind",row.role));header.append(drag);
      this.preview=node("p","patrol-source-summary",row.summary);this.detail=node("pre","patrol-source-detail");this.detail.hidden=true;
      this.expand=action(row.truncated?"展开完整内容":"查看内容与结构",()=>view.controller.expand(row.source_row_id),"patrol-source-expand");
      this.reason=node("p","patrol-source-reason",row.exclusion_reason);this.reason.hidden=row.eligible;
      this.node.append(header,this.preview,this.expand,this.detail,this.reason);this.checkbox.disabled=!row.eligible;drag.disabled=!row.eligible;this.expand.hidden=!row.eligible;
    }
    sync(){
      const controller=this.view.controller,id=this.row.source_row_id;
      this.checkbox.checked=controller.selected.has(id);this.node.classList.toggle("is-selected",this.checkbox.checked);
      const expanded=controller.expanded.has(id);this.detail.hidden=!expanded;this.preview.hidden=expanded;
      this.expand.textContent=expanded?"收起内容":this.row.truncated?"展开完整内容":"查看内容与结构";
      if(expanded){const entry=controller.details.get(id);if(entry!==this.detailEntry || !this.detail.firstChild){
        this.detailEntry=entry;const raw=entry?JSON.stringify({kind:entry.kind,payload:entry.payload},null,2):"读取完整内容…";
        if(raw.length>32000){this.port ||= root.FocusPatrolJsonEditor.createJsonEditor({label:"只读来源完整内容",wrap:true});this.port.readOnly=true;this.port.value=raw;this.detail.replaceChildren(this.port.element);}
        else {this.port?.destroy();this.port=null;this.detail.textContent=raw;}
      }if(this.port)this.port.refresh();}
    }
    refresh(){}
    destroy(){this.port?.destroy();}
  }
  class PatrolSourceBrowser {
    constructor(controller,options){
      this.controller=controller;this.options=options;this.node=node("section","patrol-source-pane");this.node.setAttribute("aria-label","只读来源浏览");
      this._controls();this.list=node("div","patrol-source-list");this.list.setAttribute("aria-label","来源条目");
      this.empty=node("div","patrol-source-empty","选择一个精确版本，挑出需要的内容");
      this.viewport=new root.FocusPatrolViewport.PatrolViewport(null,this.list,{items:()=>controller.rows,key:row=>row.source_row_id,create:row=>new SourceRow(this,row),estimate:150,gap:0,stageClass:"patrol-source-stage"});
      this.pagination=node("div","patrol-source-pagination");this.previous=action("上一页",()=>{this.list.scrollTop=0;controller.previousPage();});this.next=action("下一页",()=>{this.list.scrollTop=0;controller.nextPage();});this.page=node("span");this.pagination.append(this.previous,this.page,this.next);
      this.node.append(this.controls,this.status,this.list,this.empty,this.pagination,this.selection);
      controller.onChange=()=>this.render();this.render();
    }
    _controls(){
      const controller=this.controller;this.controls=node("div","patrol-source-controls");
      const heading=node("header","patrol-pane-heading");heading.append(node("h3","","来源"));
      this.kind=node("select");this.kind.setAttribute("aria-label","来源类型");
      for(const [value,label]of [["context","会话 Context"],["patrol","小兵 Patrol"],["file","工作区文件"],["material","材料版本"]]){const option=node("option","",label);option.value=value;this.kind.append(option);}
      this.kind.value=controller.kind;
      this.kind.onchange=()=>{clearTimeout(this.searchTimer);clearTimeout(this.directoryTimer);controller.setKind(this.kind.value);};
      this.catalog=node("select");this.catalog.setAttribute("aria-label","精确来源版本");this.catalog.onchange=()=>{
        clearTimeout(this.searchTimer);
        if(this.catalog.value===""){controller.choose(null);return;}
        const item=controller.catalog[Number(this.catalog.value)];if(!item)return;
        if(controller.kind==="material")controller.materialVersions(item.source_ref);else controller.choose(item.source_ref);
      };
      this.catalogMore=action("更多版本",()=>controller.kind==="material" && controller.materialRef?controller.materialVersions(controller.materialRef,true):controller.directory(controller.kind,true));
      this.catalogSearch=node("input");this.catalogSearch.type="search";this.catalogSearch.placeholder="搜索会话或小兵名称";this.catalogSearch.setAttribute("aria-label","搜索来源版本");this.catalogSearch.value=controller.catalogQuery;
      this.searchCatalog=action("⌕",()=>{this.catalogSearchOpen=!this.catalogSearchOpen;this.render();if(this.catalogSearchOpen)this.catalogSearch.focus();});this.searchCatalog.setAttribute("aria-label","搜索来源版本名称");
      heading.append(this.kind,this.searchCatalog);
      this.catalogSearch.oninput=()=>{clearTimeout(this.directoryTimer);this.directoryTimer=setTimeout(()=>{controller.catalogQuery=this.catalogSearch.value;controller.directory(controller.kind);},250);};
      this.versions=node("select");this.versions.setAttribute("aria-label","材料精确版本");this.versions.onchange=()=>{clearTimeout(this.searchTimer);controller.choose(this.versions.value===""?null:controller.versions[Number(this.versions.value)]?.source_ref);};
      this.path=node("input");this.path.placeholder="相对工作区路径，如 src/main.py";this.path.setAttribute("aria-label","来源文件路径");
      this.fileOpen=action("预览文件",()=>controller.choose({kind:"file",context_id:controller.draft.task_id,path:this.path.value}));
      this.path.onkeydown=event=>{if(event.key==="Enter"){event.preventDefault();this.fileOpen.click();}};
      this.search=node("input");this.search.type="search";this.search.placeholder="搜索来源正文";this.search.setAttribute("aria-label","搜索来源正文");this.search.oninput=()=>{clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>controller.search(this.search.value),250);};
      const options=node("div","patrol-source-options");const label=node("label","","历史可读内容");this.historical=node("input");this.historical.type="checkbox";this.historical.checked=controller.historical;this.historical.onchange=()=>controller.setHistorical(this.historical.checked);label.prepend(this.historical);
      options.append(label,action("刷新",()=>controller.ref?controller.refresh():controller.kind==="file"?this.fileOpen.click():controller.directory()));
      this.status=node("p","patrol-source-status");this.status.setAttribute("role","status");
      this.selection=node("footer","patrol-source-selection");this.count=node("span");this.count.setAttribute("role","status");this.clear=action("清空选择",()=>controller.clear());this.insert=action("插入到末尾",()=>this.options.onInsert(),"patrol-source-insert");this.selection.append(this.count,this.clear,this.insert);
      this.controls.append(heading,this.catalogSearch,this.catalog,this.catalogMore,this.versions,this.path,this.fileOpen,this.search,options);
    }
    render(){
      const c=this.controller,isFile=c.kind==="file",signature=JSON.stringify(c.catalog);
      this.kind.value=c.kind;this.catalog.hidden=isFile;this.catalogMore.hidden=isFile || !(c.kind==="material" && c.materialRef?c.materialNext:c.catalogNext);this.path.hidden=this.fileOpen.hidden=!isFile;
      this.catalogMore.textContent=c.kind==="material"?c.materialRef?"更早的材料版本":"更多材料":"更多版本";
      this.searchCatalog.hidden=isFile || c.kind==="material";this.searchCatalog.setAttribute("aria-expanded",String(Boolean(this.catalogSearchOpen)));
      this.catalogSearch.hidden=isFile || c.kind==="material" || !this.catalogSearchOpen && !c.catalogQuery;
      if(document.activeElement!==this.catalogSearch)this.catalogSearch.value=c.catalogQuery;
      this.versions.hidden=c.kind!=="material" || !c.versions;
      if(signature!==this._catalogSignature){
        this._catalogSignature=signature;const placeholder=node("option","",c.catalogLoading?"读取版本…":"选择精确来源版本");placeholder.value="";this.catalog.replaceChildren(placeholder);
        c.catalog.forEach((item,index)=>{const option=node("option","",item.label);option.value=String(index);option.disabled=!item.available;this.catalog.append(option);});
      }
      if(c.ref && !isFile){const index=c.catalog.findIndex(item=>item.source_ref.revision_id===c.ref.revision_id && item.source_ref.run_id===c.ref.run_id && item.source_ref.material_id===c.ref.material_id);this.catalog.value=index<0?"":String(index);}else this.catalog.value="";
      if(c.versions!==this._versions){this._versions=c.versions;const placeholder=node("option","","选择材料精确版本");placeholder.value="";this.versions.replaceChildren(placeholder);c.versions?.forEach((item,index)=>{const option=node("option","",item.label);option.value=String(index);this.versions.append(option);});}
      if(c.versions && c.ref){const index=c.versions.findIndex(item=>item.source_ref.version_id===c.ref.version_id);this.versions.value=index<0?"":String(index);}
      if(document.activeElement!==this.search)this.search.value=c.query;this.search.disabled=!c.ref;this.historical.checked=c.historical;
      this.status.textContent=c.error || (c.loading || c.catalogLoading?"读取来源…":c.source?`${c.source.title || c.source.path || "冻结 checkpoint"} · ${c.total} 项${c.excluded?` · ${c.excluded} 项不可导入`:""}`:"");
      this.status.classList.toggle("is-error",Boolean(c.error));this.status.hidden=!this.status.textContent;
      this.empty.hidden=Boolean(c.rows.length);this.empty.textContent=c.ref?c.loading?"正在读取内容…":c.error?"来源暂不可用，可以刷新或切换版本":"此查询没有来源条目":"选择一个精确版本，挑出需要的内容";
      this.list.hidden=!c.rows.length;this.pagination.hidden=!c.ref || !c.total;this.previous.disabled=c.loading || !c.page;this.next.disabled=c.loading || !c.next;this.page.textContent=`第 ${c.page+1} 页`;
      this.count.textContent=`已选 ${c.selected.size} 项`;this.clear.disabled=!c.selected.size;this.insert.disabled=!c.selected.size || c.loading || this.busy;
      this.viewport.schedule();
    }
    setAnchor(id,index){this.insert.textContent=id==null?"插入到末尾":`插入到第 ${index+1} 项前`;}
    destroy(){clearTimeout(this.searchTimer);clearTimeout(this.directoryTimer);this.viewport.destroy();this.controller.onChange=()=>{};}
  }
  root.FocusPatrolSourceBrowser={PatrolSourceBrowser};
})(window);
