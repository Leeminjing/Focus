/* 本文件对外提供 PatrolViewport 的有界列表呈现，供目标条目及只读来源复用。
 * 输入为文档或 items/key/create 视图端口与滚动容器；输出为最多 80 个挂载节点、120 个缓存、可变高度及活跃编辑 pin。
 * 工作流以测量高度缓存计算位置，只挂载窗口与当前焦点条目；尺寸或结构变化按可见条目身份恢复滚动锚点，观察器测量与下一帧写布局分离。
 * 示例：viewport.render(); viewport.locate('item-id')。
 */
(function(global){
  "use strict";
  class PatrolViewport{
    constructor(wb,list,options={}){this.wb=wb;this.list=list;this.items=options.items || (()=>wb.doc.value.entries);this.key=options.key || (entry=>entry.entry_id);
      this.create=options.create || (entry=>global.FocusPatrolCards.create(wb,entry));this.estimate=options.estimate || 150;this.gap=options.gap ?? 8;
      this.heights=new Map();this.widths=new Map();this.pendingRefresh=new Set();this.offsets=[];this.cache=new Map();this.stage=document.createElement("div");this.stage.className=options.stageClass || "patrol-card-stage";list.append(this.stage);
      this.observer=new ResizeObserver(records=>this._measure(records));
      this._scroll=()=>this.schedule();list.addEventListener("scroll",this._scroll);
    }
    schedule(){if(!this.frame)this.frame=requestAnimationFrame(()=>{this.frame=null;for(const id of this.pendingRefresh)this.cache.get(id)?.refresh();this.pendingRefresh.clear();this.render();});}
    _measure(records){
      const anchor=this._anchor();
      let changed=false;
      for(const record of records){
        const id=record.target.dataset.viewportId,box=record.borderBoxSize[0],height=box?.blockSize || record.target.offsetHeight,width=box?.inlineSize || record.target.offsetWidth;
        if(width>0 && this.widths.get(id)!==width){this.widths.set(id,width);this.pendingRefresh.add(id);changed=true;}
        if(height>0 && Math.abs((this.heights.get(id)||0)-height)>1){this.heights.set(id,height);changed=true;}
      }
      if(changed){this.pendingAnchor ||= anchor;this.schedule();}
    }
    _anchor(){const index=this.indexAt(this.list.scrollTop),id=this.renderedKeys?.[index];return id?{id,within:this.list.scrollTop-this.offsets[index],scroll:this.list.scrollTop}:null;}
    indexAt(offset){let lo=0,hi=this.offsets.length-1;while(lo<hi){const mid=Math.floor((lo+hi+1)/2);if(this.offsets[mid]<=offset)lo=mid;else hi=mid-1;}return lo;}
    render(){const entries=this.items(),keys=entries.map(entry=>this.key(entry)),positions=new Map(keys.map((id,index)=>[id,index]));
      const structureChanged=this.renderedKeys && (keys.length!==this.renderedKeys.length || keys.some((id,index)=>id!==this.renderedKeys[index]));
      const anchor=this.pendingAnchor || (structureChanged?this._anchor():null);this.pendingAnchor=null;
      let top=0;this.offsets=[];for(const entry of entries){this.offsets.push(top);top+=(this.heights.get(this.key(entry))||this.estimate)+this.gap;}this.stage.style.height=top+"px";
      if(anchor && positions.has(anchor.id) && Math.abs(this.list.scrollTop-anchor.scroll)<2)this.list.scrollTop=Math.max(0,this.offsets[positions.get(anchor.id)]+anchor.within);
      this.renderedKeys=keys;
      const start=Math.max(0,this.indexAt(this.list.scrollTop)-3),end=Math.min(entries.length,start+Math.min(79,Math.ceil((this.list.clientHeight||600)/90)+12));
      const wanted=new Set(entries.slice(start,end).map(entry=>this.key(entry))),active=document.activeElement?.closest("[data-viewport-id]");
      const focused=this.list.contains(active)?active?.dataset.viewportId:null;if(focused && positions.has(focused))wanted.add(focused);
      for(const [id,card]of this.cache)if(!wanted.has(id) && card.node.isConnected){this.observer.unobserve(card.node);card.node.remove();}
      this.wb?.cards.clear();
      for(const id of wanted){const index=positions.get(id),entry=entries[index];if(!entry)continue;let card=this.cache.get(id);
        if(card && (card.entry!==entry || card.kind!==entry.kind)){this.observer.unobserve(card.node);card.node.remove();card.destroy();this.cache.delete(id);card=null;}
        if(!card){card=this.create(entry);card.entry=entry;card.kind=entry.kind;card.node.dataset.viewportId=id;this.cache.set(id,card);}
        card.node.style.transform=`translateY(${this.offsets[index]}px)`;card.sync(index);if(!card.node.isConnected){this.stage.append(card.node);this.observer.observe(card.node);card.refresh();}this.wb?.cards.set(id,card.node);
      }
      this._order([...wanted]);
      while(this.cache.size>120){const victim=[...this.cache.keys()].find(id=>!wanted.has(id));if(!victim)break;this.cache.get(victim).destroy();this.cache.delete(victim);}
      for(const id of this.heights.keys())if(!positions.has(id)){this.heights.delete(id);this.widths.delete(id);}
      this.empty ||= document.createElement("div");this.empty.className="patrol-empty";this.empty.innerHTML='<span class="patrol-empty-mark">＋</span><h3>编写 Patrol 的上下文</h3><p>添加消息、调用或结果，也可以导入已有 Context。</p>';
      if(this.wb && !entries.length && !this.empty.isConnected)this.list.append(this.empty);else if(entries.length || !this.wb)this.empty.remove();
    }
    _order(ids){
      const positions=new Map(this.items().map((entry,index)=>[this.key(entry),index]));
      const cards=ids.sort((left,right)=>positions.get(left)-positions.get(right)).map(id=>this.cache.get(id)?.node).filter(Boolean);
      if(cards.every((card,index)=>this.stage.children[index]===card))return;
      const active=cards.find(card=>card.contains(document.activeElement));
      if(active){
        const index=cards.indexOf(active);
        for(const card of cards.slice(0,index))this.stage.insertBefore(card,active);
        for(const card of cards.slice(index+1))this.stage.append(card);
      }else cards.forEach((card,index)=>{if(this.stage.children[index]!==card)this.stage.insertBefore(card,this.stage.children[index] || null);});
    }
    locate(id,field=null){const index=this.items().findIndex(entry=>this.key(entry)===id);if(index<0)return;this.pendingAnchor=null;this.list.scrollTop=Math.max(0,this.offsets[index]-16);this.render();const card=this.cache.get(id);
      const binding=field?card?.fieldEditor?.(field):["content","output","arguments","input","payload"].map(key=>card?.fieldEditor?.(key)).find(Boolean);if(binding?.focus)binding.focus();else card?.node.querySelector('textarea,input:not([hidden]),.cm-content')?.focus();}
    suspend(){cancelAnimationFrame(this.frame);this.frame=null;this.list.removeEventListener("scroll",this._scroll);this.observer.disconnect();}
    destroy(){this.suspend();for(const card of this.cache.values())card.destroy();this.cache.clear();}
  }
  global.FocusPatrolViewport={PatrolViewport};
})(window);
