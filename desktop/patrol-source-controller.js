/* 本文件对外提供 PatrolSourceController 的 setKind、choose、materialVersions 及精确来源查询/选择端口。
 * 输入为草稿身份、只读 API 与状态通知；输出为目录/投影行、版本绑定选择、详情和导入请求，不拥有作者文档。
 * 具体工作流为类型/选择意图统一使旧请求失效 → 有界查询 → 精确预览 → 跨页选择 → 输出指纹绑定插入意图；材料版本完成不能恢复已取消的选择。
 * 示例：await controller.setKind('material'); await controller.materialVersions(ref); const request=controller.insertion('entry-id')。
 */
(function(root){
  "use strict";
  class PatrolSourceController {
    constructor({draft,api,onChange=()=>{}}){
      this.draft=draft;this.api=api;this.onChange=onChange;this.kind="context";this.catalog=[];this.rows=[];
      this.selected=new Set();this.details=new Map();this.expanded=new Set();this.ref=null;this.query="";this.catalogQuery="";
      this.historical=false;this.page=0;this.cursors=[null];this.request=0;this.catalogRequest=0;this.detailRequest=0;
    }
    _emit(){this.onChange(this);}
    report(message){this.error=message;this._emit();}
    imported(ids,fingerprint){if(this.fingerprint===fingerprint)for(const id of ids)this.selected.delete(id);this._emit();}
    setKind(kind){
      this.kind=kind;this.catalog=[];this.catalogNext=null;this.catalogOffset=0;this.catalogQuery="";
      this.choose(null);return kind==="file"?Promise.resolve():this.directory();
    }
    async directory(kind=this.kind,append=false){
      if(kind!==this.kind)return this.setKind(kind);
      const ticket=++this.catalogRequest;this.catalogLoading=true;this.error=null;this._emit();
      try{
        let items,next,nextOffset;
        if(kind==="material"){
          const result=await this.api(`/desktop/api/tasks/${this.draft.task_id}/materials`);
          const offset=append?this.catalogOffset || 0:0;nextOffset=offset+50;
          items=result.slice(offset,nextOffset).map(material=>({label:material.relative_path,available:true,source_ref:{kind,context_id:this.draft.task_id,material_id:material.material_id}}));next=nextOffset<result.length?"more":null;
        }else{
          const query=new URLSearchParams({kind,limit:"50",query:this.catalogQuery});if(append && this.catalogNext)query.set("cursor",this.catalogNext);
          const result=await this.api(`/desktop/api/patrol/sources?${query}`);items=result.items || [];next=result.next_cursor;
        }
        if(ticket!==this.catalogRequest)return;
        this.catalog=append?[...this.catalog,...items].slice(-100):items;this.catalogNext=next || null;this.catalogOffset=nextOffset;
      }catch(error){if(ticket===this.catalogRequest)this.error=error.message;}
      finally{if(ticket===this.catalogRequest){this.catalogLoading=false;this._emit();}}
    }
    async materialVersions(ref,append=false){
      if(!append){this.kind="material";this.choose(null);}
      const ticket=++this.catalogRequest;this.catalogLoading=true;this._emit();
      try{
        const versions=await this.api(`/desktop/api/materials/${ref.material_id}/versions`);
        if(ticket!==this.catalogRequest)return;
        const offset=append?this.materialOffset || 0:0;this.materialRef=ref;this.materialOffset=offset+50;this.materialNext=this.materialOffset<versions.length;
        this.versions=[...(append?[]:[{label:"当前文件快照",source_ref:ref}]),...versions.slice(offset,this.materialOffset).map(version=>({label:`版本 ${version.version_id.slice(0,8)}`,source_ref:{...ref,version_id:version.version_id}}))];
        if(!append)this.choose(ref);
      }catch(error){if(ticket===this.catalogRequest)this.error=error.message;}
      finally{if(ticket===this.catalogRequest){this.catalogLoading=false;this._emit();}}
    }
    choose(ref){
      this.catalogRequest++;this.catalogLoading=false;
      if(ref)this.kind=ref.kind;
      if(!ref || ref.kind!=="material"){this.materialRef=null;this.versions=null;this.materialNext=false;this.materialOffset=0;}
      this.request++;this.detailRequest++;this.loading=false;this.ref=ref;this.source=null;this.fingerprint=null;this.rows=[];this.selected.clear();
      this.details.clear();this.expanded.clear();this.page=0;this.cursors=[null];this.next=null;this.total=0;this.excluded=0;this.error=null;this.query="";this._emit();
      return this.load();
    }
    setHistorical(value){this.detailRequest++;this.historical=value;this.selected.clear();this.details.clear();this.expanded.clear();this.fingerprint=null;return this.search(this.query);}
    search(query){this.query=query;this.page=0;this.cursors=[null];return this.load();}
    refresh(){this.page=0;this.cursors=[null];return this.load();}
    async load(){
      if(!this.ref)return;
      const ticket=++this.request;this.loading=true;this.error=null;this._emit();
      try{
        const result=await this.api(`/desktop/api/drafts/${this.draft.draft_id}/source-preview`,{method:"POST",body:JSON.stringify({
          ...this.ref,query:this.query,cursor:this.cursors[this.page],limit:50,include_historical:this.historical})});
        if(ticket!==this.request)return;
        if(this.fingerprint && result.source_fingerprint!==this.fingerprint){
          this.detailRequest++;
          this.selected.clear();this.details.clear();this.expanded.clear();this.error="来源已变化，选择已清空，请重新确认";
        }
        this.source=result.source_ref;this.fingerprint=result.source_fingerprint;this.rows=result.rows;
        this.total=result.total;this.excluded=result.excluded_count;this.next=result.next_cursor;
      }catch(error){if(ticket===this.request)this.error=error.message;}
      finally{if(ticket===this.request){this.loading=false;this._emit();}}
    }
    nextPage(){if(!this.next || this.loading)return;this.cursors[++this.page]=this.next;return this.load();}
    previousPage(){if(!this.page || this.loading)return;this.page--;return this.load();}
    select(id,value){const row=this.rows.find(row=>row.source_row_id===id);if(!row?.eligible)return;if(value)this.selected.add(id);else this.selected.delete(id);this._emit();}
    clear(){this.selected.clear();this._emit();}
    async expand(id){
      if(this.expanded.has(id)){this.expanded.delete(id);this._emit();return;}
      this.expanded.add(id);this._emit();if(this.details.has(id))return;
      const ref=this.ref,fingerprint=this.fingerprint,ticket=this.detailRequest;
      try{
        const result=await this.api(`/desktop/api/drafts/${this.draft.draft_id}/source-preview`,{method:"POST",body:JSON.stringify({...ref,row_id:id,include_historical:this.historical})});
        if(ticket!==this.detailRequest || ref!==this.ref)return;
        if(fingerprint!==result.source_fingerprint){this.error="来源已变化，请刷新预览后重新确认";this._emit();return;}
        this.details.set(id,result.row.entry || null);
        while(this.details.size>24){const oldest=this.details.keys().next().value;this.details.delete(oldest);this.expanded.delete(oldest);}
        this._emit();
      }catch(error){if(ticket===this.detailRequest){this.error=error.message;this._emit();}}
    }
    insertion(beforeId){
      if(!this.ref || !this.fingerprint || !this.selected.size)throw new Error("请先选择来源条目");
      return {...this.source,...this.ref,include_historical:this.historical,selected_row_ids:[...this.selected],source_fingerprint:this.fingerprint,before_entry_id:beforeId};
    }
    destroy(){this.request++;this.catalogRequest++;this.detailRequest++;this.onChange=()=>{};this.details.clear();}
  }
  root.FocusPatrolSource={PatrolSourceController};if(typeof module!=="undefined")module.exports=root.FocusPatrolSource;
})(typeof window!=="undefined"?window:globalThis);
