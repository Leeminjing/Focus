/* 本文件对外提供 PatrolInteractions 的稳定锚点编排与导入协调。
 * 输入为唯一文档、保存队列、来源控制器和指针/键盘意图；输出为一次结构操作、插入反馈或保留本地编辑的恢复状态。
 * 具体工作流为选择稳定锚点 → 显示插入线 → 串行导入并按 ID 合并；等待期间字段继续编辑、结构命令排队，不确定结果先读取确认。
 * 示例：interactions.setAnchor('e1'); await interactions.importSelected(); interactions.run(()=>doc.move('e2',0))。
 */
(function(root){
  "use strict";
  class PatrolInteractions {
    constructor(wb){this.wb=wb;this.anchor=null;this.pending=[];this.busy=false;}
    run(action){if(this.busy){this.pending.push(action);this.wb.status.textContent="导入中 · 编排操作将在完成后执行";return;}return action();}
    setAnchor(id){this.anchor=id;this.wb.view?.update();for(const [entryId,card]of this.wb.cards)card.classList.toggle("is-insertion-anchor",entryId===id);}
    async importSelected(request=null){
      if(this.busy)return null;
      const wb=this.wb,controller=wb.sourceController;
      try{request ||= controller.insertion(this.anchor);}catch(error){controller.report(error.message);return null;}
      if(wb.composing){controller.report("请完成当前输入后再插入");return null;}
      this.busy=true;wb.sourceView.busy=true;wb.sourceView.render();controller.error=null;
      try{
        const result=await wb.queue.mutate(async()=>{
          const result=await wb.api(`/desktop/api/drafts/${wb.draft.draft_id}/sources`,{method:"POST",body:JSON.stringify({...request,draft_revision:wb.queue.serverRevision})});
          wb.queue.serverRevision=result.draft_revision;wb.draft.draft_revision=result.draft_revision;
          const entries=result.inserted_entries || result.authoring_document?.entries.filter(entry=>!wb.doc.byId.has(entry.entry_id)) || [];
          wb.doc.insertBatch(entries,request.before_entry_id);return result;
        });
        controller.imported(request.selected_row_ids,request.source_fingerprint);
        wb.status.textContent=`已插入 ${result.inserted_entry_ids?.length ?? 0} 项 · 保留本地编辑`;wb.renderList();wb._refreshSources();return result;
      }catch(error){
        controller.error=error.detail?.message || error.message;wb.status.textContent=controller.error;
        if(error.code==="draft_revision_conflict"){wb.queue.blocked=error;wb.localConflict=true;}
        else if(!error.status || error.status>=500){
          wb.queue.blocked=error;
          try{
            const remote=await wb.api(`/desktop/api/drafts/${wb.draft.draft_id}`);
            if(remote.draft_revision===wb.queue.serverRevision)wb.queue.blocked=null;
            else {wb.localConflict=true;controller.error="导入结果已读取，服务端版本已变化；本地编辑保留，请载入服务器版本（可撤销）";}
          }catch(_){controller.error="导入结果暂无法确认；本地编辑已保留，请读取服务器版本后继续";}
        }
        return null;
      }finally{
        this.busy=false;wb.sourceView.busy=false;controller.report(controller.error);
        const pending=this.pending.splice(0);for(const action of pending)action();
        if(!wb.queue.blocked && wb.queue.confirmed<wb.queue.localRevision)wb.queue.flush().catch(()=>{});
      }
    }
    startTarget(event,id){this._start(event,{id,type:"target"});}
    startSource(event){
      if(!this.wb.sourceController.selected.size)return;
      this._start(event,{type:"source",request:this.wb.sourceController.insertion(null)});
    }
    _start(event,value){
      if(this.busy)return;event.preventDefault();this.cancel();const wb=this.wb;
      this.drag={...value,x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,started:false};
      this.line=root.FocusPatrolUI.node("div","patrol-insertion-line",value.type==="source"?`插入 ${value.request.selected_row_ids.length} 项`:"移动到此处");wb.list.append(this.line);this.line.hidden=true;
      const move=event=>{if(this.drag){this.drag.x=event.clientX;this.drag.y=event.clientY;}};
      const finish=event=>{
        const drag=this.drag;this.cancel();if(event.type!=="pointerup" || !drag?.started || !drag.inside)return;
        this.setAnchor(drag.anchor);
        if(drag.type==="source")this.importSelected({...drag.request,before_entry_id:drag.anchor});
        else this.moveBefore(drag.id,drag.anchor);
      };
      this.events=new AbortController();const signal=this.events.signal;
      document.addEventListener("pointermove",move,{signal});document.addEventListener("pointerup",finish,{signal});document.addEventListener("pointercancel",finish,{signal});
      wb._cancelDrag=()=>this.cancel();this._tick();
    }
    _tick(){
      const drag=this.drag;if(!drag)return;const wb=this.wb,box=wb.list.getBoundingClientRect();
      drag.started ||= Math.abs(drag.x-drag.startX)+Math.abs(drag.y-drag.startY)>4;
      drag.inside=drag.x>=box.left && drag.x<=box.right && drag.y>=box.top && drag.y<=box.bottom;
      this.line.hidden=!drag.started || !drag.inside;
      if(drag.inside && drag.started){
        const previous=wb.list.scrollTop;if(drag.y<box.top+36)wb.list.scrollTop-=12;else if(drag.y>box.bottom-36)wb.list.scrollTop+=12;
        if(previous!==wb.list.scrollTop)wb.viewport.render();
        const offset=drag.y-box.top+wb.list.scrollTop,entries=wb.doc.value.entries,index=wb.viewport.indexAt(offset);
        const entry=entries[index],height=entry?(wb.viewport.heights.get(entry.entry_id)||150):0;
        const gap=entry && offset<(wb.viewport.offsets[index] || 0)+height/2?index:entry?index+1:0;
        drag.anchor=entries[gap]?.entry_id || null;const top=(wb.viewport.offsets[gap] ?? parseFloat(wb.viewport.stage.style.height)) || 0;
        this.line.style.top=`${top}px`;
      }
      this.frame=requestAnimationFrame(()=>this._tick());
    }
    moveBefore(id,anchor){this.run(()=>{const entries=this.wb.doc.value.entries,from=entries.findIndex(entry=>entry.entry_id===id);
      const before=anchor==null?entries.length:entries.findIndex(entry=>entry.entry_id===anchor);if(from<0 || before<0 || id===anchor)return;
      this.wb.doc.move(id,before>from?before-1:before);this.wb.renderList();});}
    cancel(){cancelAnimationFrame(this.frame);this.events?.abort();this.line?.remove();this.drag=null;this.wb._cancelDrag=null;}
    destroy(){this.cancel();this.pending=[];}
  }
  root.FocusPatrolInteractions={PatrolInteractions};
})(window);
