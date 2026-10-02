/* 本文件对外提供 PatrolViewport 的连续 typed 卡片呈现。
 * 输入为文档、稳定卡片工厂和滚动容器；输出为有界 DOM、可变高度顺序及活跃编辑 pin。
 * 工作流以测量高度缓存计算位置，只挂载窗口与当前焦点条目；ResizeObserver 只采集尺寸，布局刷新延迟到下一帧，避免观察回路。
 * 示例：viewport.render(); viewport.locate('item-id')。
 */
(function(global){
  "use strict";
  class PatrolViewport{
    constructor(wb,list){this.wb=wb;this.list=list;this.heights=new Map();this.widths=new Map();this.pendingRefresh=new Set();this.offsets=[];this.cache=new Map();this.stage=document.createElement("div");this.stage.className="patrol-card-stage";list.append(this.stage);
      this.observer=new ResizeObserver(records=>this._measure(records));
      list.addEventListener("scroll",()=>this.schedule());
    }
    schedule(){if(!this.frame)this.frame=requestAnimationFrame(()=>{this.frame=null;for(const id of this.pendingRefresh)this.cache.get(id)?.refresh();this.pendingRefresh.clear();this.render();});}
    _measure(records){
      let changed=false;
      for(const record of records){
        const id=record.target.dataset.entryId,box=record.borderBoxSize[0],height=box?.blockSize || record.target.offsetHeight,width=box?.inlineSize || record.target.offsetWidth;
        if(width>0 && this.widths.get(id)!==width){this.widths.set(id,width);this.pendingRefresh.add(id);changed=true;}
        if(height>0 && Math.abs((this.heights.get(id)||0)-height)>1){this.heights.set(id,height);changed=true;}
      }
      if(changed)this.schedule();
    }
    indexAt(offset){let lo=0,hi=this.offsets.length-1;while(lo<hi){const mid=Math.floor((lo+hi+1)/2);if(this.offsets[mid]<=offset)lo=mid;else hi=mid-1;}return lo;}
    render(){const entries=this.wb.doc.value.entries;let top=0;this.offsets=[];for(const entry of entries){this.offsets.push(top);top+=(this.heights.get(entry.entry_id)||150)+12;}this.stage.style.height=top+"px";
      const start=Math.max(0,this.indexAt(this.list.scrollTop)-3),end=Math.min(entries.length,start+Math.min(80,Math.ceil((this.list.clientHeight||600)/90)+12));
      const wanted=new Set(entries.slice(start,end).map(e=>e.entry_id));const focused=document.activeElement?.closest(".patrol-entry")?.dataset.entryId;if(focused && this.wb.doc.byId.has(focused))wanted.add(focused);
      for(const [id,card]of this.cache)if(!wanted.has(id) && card.node.isConnected){this.observer.unobserve(card.node);card.node.remove();}
      this.wb.cards.clear();
      for(const id of wanted){const index=entries.findIndex(e=>e.entry_id===id),entry=entries[index];if(!entry)continue;let card=this.cache.get(id);
        if(card && (card.entry!==entry || card.kind!==entry.kind)){this.observer.unobserve(card.node);card.node.remove();card.destroy();this.cache.delete(id);card=null;}
        if(!card){card=global.FocusPatrolCards.create(this.wb,entry);card.entry=entry;card.kind=entry.kind;this.cache.set(id,card);}
        card.node.style.transform=`translateY(${this.offsets[index]}px)`;card.sync(index);if(!card.node.isConnected){this.stage.append(card.node);this.observer.observe(card.node);card.refresh();}this.wb.cards.set(id,card.node);
      }
      this._order([...wanted]);
      while(this.cache.size>120){const victim=[...this.cache.keys()].find(id=>!wanted.has(id));if(!victim)break;this.cache.get(victim).destroy();this.cache.delete(victim);}
      this.empty ||= document.createElement("div");this.empty.className="patrol-empty";this.empty.innerHTML='<span class="patrol-empty-mark">＋</span><h3>编写 Patrol 的上下文</h3><p>添加消息、调用或结果，也可以导入已有 Context。</p>';
      if(!entries.length && !this.empty.isConnected)this.list.append(this.empty);else if(entries.length)this.empty.remove();
    }
    _order(ids){
      const positions=new Map(this.wb.doc.value.entries.map((entry,index)=>[entry.entry_id,index]));
      const cards=ids.sort((left,right)=>positions.get(left)-positions.get(right)).map(id=>this.cache.get(id)?.node).filter(Boolean);
      if(cards.every((card,index)=>this.stage.children[index]===card))return;
      const active=cards.find(card=>card.contains(document.activeElement));
      if(active){
        const index=cards.indexOf(active);
        for(const card of cards.slice(0,index))this.stage.insertBefore(card,active);
        for(const card of cards.slice(index+1))this.stage.append(card);
      }else cards.forEach((card,index)=>{if(this.stage.children[index]!==card)this.stage.insertBefore(card,this.stage.children[index] || null);});
    }
    locate(id){const index=this.wb.doc.value.entries.findIndex(e=>e.entry_id===id);if(index<0)return;this.list.scrollTop=Math.max(0,this.offsets[index]-16);this.render();const card=this.cache.get(id);card?.node.querySelector('textarea,input:not([hidden]),.cm-content')?.focus();}
    destroy(){cancelAnimationFrame(this.frame);this.observer.disconnect();for(const card of this.cache.values())card.destroy();}
  }
  global.FocusPatrolViewport={PatrolViewport};
})(window);
