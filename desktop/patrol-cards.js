/* 本文件对外提供 FocusPatrolCards.create 的紧凑 typed 条目视图。
 * 输入为 authoring kind/payload、文档与异步解析端口；输出为稳定身份的卡片和正文/结构编辑端口。
 * 工作流按 kind 选择字段，PayloadField 管理自然高度原位控件，ItemCard 管理菜单、来源、稳定插入锚点与局部同步；结构动作经交互端口串行。
 * 合法结构改写协调文本/内容块模式，只升级变化字段的控件并保留选区；输入法及待解析正文保持当前输入。
 * 大型/结构内容按需采用可见区域编辑器；不会将未知字段或来源声明转成执行权威。
 * 示例：create(workbench, entry).fieldEditor('arguments') 直接编辑独立 function_call 参数。
 */
(function(global){
  "use strict";
  const node=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls || "";if(text!=null)n.textContent=text;return n;};
  const action=(label,fn)=>{const n=node("button","patrol-icon-action",label);n.type="button";n.onclick=fn;return n;};
  class PayloadField {
    constructor(wb,entry,key,label,multiline){
      this.wb=wb;this.entry=entry;this.key=key;this.multiline=multiline;this.label=label;
      const value=entry.payload[key] ?? "";this.renderedValue=value;this.structured=typeof value!=="string";
      this.node=node("label","patrol-field");
      if(label!=="条目正文")this.node.append(node("span","patrol-field-label",label));
      if(this.structured || value.length>32000){
        this.port=global.FocusPatrolJsonEditor.createJsonEditor({label,wrap:multiline});this.input=this.port.element;
        this.port.value=entry.content_buffer || (this.structured?JSON.stringify(value,null,2):value);this.node.classList.add("is-structured");
      }else{
        this.input=node(multiline?"textarea":"input","patrol-inline-input");this.input.value=value;
        if(multiline){this.input.rows=1;this.input.spellcheck=false;}
      }
      this._bindInput();this.node.append(this.input);
    }
    _bindInput(){
      this.input.setAttribute("aria-label",this.label);this.input.dataset.payloadField=this.key;
      this.input.addEventListener("input",event=>this._input(event));
    }
    _upgradeEditor(raw){
      const previous=this.input,active=document.activeElement===previous;
      const selection=active?[previous.selectionStart,previous.selectionEnd]:null;
      this.port=global.FocusPatrolJsonEditor.createJsonEditor({label:this.label,wrap:this.multiline});this.input=this.port.element;
      this.port.value=raw;this.node.classList.add("is-structured");this._bindInput();previous.replaceWith(this.input);
      if(active){this.port.focus();this.port.setSelectionRange(Math.min(selection[0],raw.length),Math.min(selection[1],raw.length));}
    }
    _input(event){
      if(this.port && event.target!==this.input)return;
      const raw=this.port?this.port.value:this.input.value;
      if(this.structured){
        this.entry.content_buffer=raw;this.entry.content_error="内容正在解析";this.wb._changed();
        this.wb._parseEntry(this.entry,"content",raw,structuredClone(this.entry),()=>{this.renderedValue=this.entry.payload[this.key];},this.key);
      }else {this.wb.doc.setPayloadField(this.entry.entry_id,this.key,raw);this.renderedValue=raw;}
      this.refresh();
    }
    sync(){
      const value=this.entry.payload[this.key] ?? "",raw=typeof value==="string"?value:JSON.stringify(value,null,2);
      if(this.wb.composing || this.entry.content_error)return;
      this.structured=typeof value!=="string";
      if(!this.port && (this.structured || raw.length>32000))this._upgradeEditor(raw);
      if(this.port){
        if(this.renderedValue!==value){
          const selection=this.port.hasFocus?[this.port.selectionStart,this.port.selectionEnd]:null;
          this.port.value=raw;this.renderedValue=value;if(selection)this.port.setSelectionRange(Math.min(selection[0],raw.length),Math.min(selection[1],raw.length));
        }
      }
      else if(this.input.value!==raw){
        const active=document.activeElement===this.input,selection=active?[this.input.selectionStart,this.input.selectionEnd,this.input.selectionDirection]:null;
        this.input.value=raw;this.renderedValue=value;if(selection)this.input.setSelectionRange(Math.min(selection[0],raw.length),Math.min(selection[1],raw.length),selection[2]);this.refresh();
      }
    }
    refresh(){
      if(this.port){this.port.refresh();return;}
      if(this.multiline){this.input.style.height="auto";this.input.style.height=Math.max(32,this.input.scrollHeight)+"px";}
    }
    destroy(){this.port?.destroy();}
  }
  class ItemCard {
    constructor(wb,entry){
      this.wb=wb;this.entry=entry;this.kind=entry.kind;this.bindings=new Map();
      this.node=node("article","patrol-entry");this.node.dataset.entryId=entry.entry_id;this.node.tabIndex=0;
      this._heading();this._body();this._structure();this._source();
      this.error=node("p","patrol-item-error");this.node.append(this.error);
      this.node.addEventListener("focusin",()=>{wb.selected=entry.entry_id;});this.sync(0);
    }
    _heading(){
      const header=node("header","patrol-card-heading");
      const drag=action("⠿",()=>{});drag.setAttribute("aria-label","拖动排序");drag.onpointerdown=e=>this.wb._drag(e,this.entry.entry_id);
      this.title=node("span","patrol-kind");this.role=node("input","patrol-role");
      this.role.setAttribute("aria-label","角色意图");this.role.setAttribute("list","patrol-roles");
      this.role.oninput=()=>this.wb.doc.setPayloadField(this.entry.entry_id,"role",this.role.value);
      this.number=node("span","patrol-card-number");
      const menu=node("details","patrol-card-menu");menu.append(node("summary","","···"));menu.firstChild.setAttribute("aria-label","条目操作");
      const actions=node("div","patrol-popover");
      actions.append(action("复制",()=>this.wb.interactions.run(()=>{const value=this.wb.doc.copy(this.entry.entry_id);this.wb.renderList();this.wb._locate(value.entry_id);})),
        action("向上",()=>this._move(-1)),action("向下",()=>this._move(1)),
        action("移动到指定位置",()=>this._position(actions)),
        action("在下方插入",()=>this.wb._add("message","user",this.wb.doc.value.entries.indexOf(this.entry)+1)),
        action("删除",()=>this.wb.interactions.run(()=>{this.wb.doc.remove(this.entry.entry_id);this.wb.renderList();})));
      menu.append(actions);header.append(drag,this.title,this.role,this.number,menu);
      const gap=action("＋ 插入到这里",()=>this.wb.interactions.setAnchor(this.entry.entry_id));gap.className="patrol-entry-gap";gap.setAttribute("aria-label","在此条目前插入来源或新增条目");
      this.node.append(gap,header);
    }
    _body(){
      this.body=node("div","patrol-card-body");this.node.append(this.body);
      if(["function_call","custom_tool_call"].includes(this.kind)){
        this._field("name","工具名称",false);this._field("call_id","call_id",false);
        this._field(this.kind==="function_call"?"arguments":"input",this.kind==="function_call"?"arguments":"input",true);
      }else if(["function_call_output","custom_tool_call_output"].includes(this.kind)){
        this._field("call_id","call_id",false);this._field("output","output",true);
      }else if(this.kind==="agent_collaboration"){
        this._field("author","发送者",false);this._field("recipient","接收者",false);this._field("content","条目正文",true);
      }else if(["message","authored_instruction","task_contract","selected_context"].includes(this.kind))this._field("content","条目正文",true);
      else this._genericPayload();
    }
    _genericPayload(){
      this.payloadEditor=global.FocusPatrolJsonEditor.createJsonEditor({label:"通用 Item payload"});
      this.renderedPayload=this.entry.payload;
      this.payloadEditor.value=this.entry.payload_buffer || JSON.stringify(this.entry.payload,null,2);
      this.body.append(this.payloadEditor.element);
      this.payloadEditor.element.addEventListener("input",event=>{
        if(event.target!==this.payloadEditor.element)return;
        const base=this.entry.payload_base || structuredClone(this.entry);this.entry.payload_base=base;
        this.entry.payload_buffer=this.payloadEditor.value;this.entry.payload_error="payload 正在解析";this.wb._changed();
        this.wb._parseEntry(this.entry,"payload",this.payloadEditor.value,base,()=>{delete this.entry.payload_base;this.renderedPayload=this.entry.payload;});
      });
    }
    _field(key,label,multiline){const field=new PayloadField(this.wb,this.entry,key,label,multiline);this.bindings.set(key,field);this.body.append(field.node);}
    _move(delta){this.wb.interactions.run(()=>{this.wb.doc.move(this.entry.entry_id,this.wb.doc.value.entries.indexOf(this.entry)+delta);this.wb.renderList();});}
    _position(container){
      if(this.positionInput)return;
      this.positionInput=node("input");this.positionInput.type="number";this.positionInput.min="1";this.positionInput.max=String(this.wb.doc.value.entries.length);this.positionInput.value=String(this.wb.doc.value.entries.indexOf(this.entry)+1);this.positionInput.setAttribute("aria-label","目标条目位置");
      const confirm=action("确认移动",()=>{const index=Number(this.positionInput.value)-1;if(Number.isInteger(index) && index>=0 && index<this.wb.doc.value.entries.length)this.wb.interactions.run(()=>{this.wb.doc.move(this.entry.entry_id,index);this.wb.renderList();});});container.append(this.positionInput,confirm);this.positionInput.focus();
    }
    _structure(){
      const fields=node("details","patrol-item-structure");fields.append(node("summary","","条目结构"));this.node.append(fields);
      fields.ontoggle=()=>{
        if(!fields.open || this.structureEditor)return;
        this.fieldsBase=this.entry.fields_base || structuredClone(this.entry);
        this.structureEditor=global.FocusPatrolJsonEditor.createJsonEditor({label:"条目结构 JSON"});
        this.structureEditor.value=this.entry.fields_buffer || JSON.stringify(this.entry,null,2);fields.append(this.structureEditor.element);
        this.structureEditor.element.addEventListener("input",event=>this._parseStructure(event));
        requestAnimationFrame(()=>this.structureEditor.refresh());
      };
    }
    _parseStructure(event){
      if(event.target!==this.structureEditor.element)return;
      this.entry.fields_base=this.fieldsBase;this.entry.fields_buffer=this.structureEditor.value;this.entry.fields_error="结构正在解析";this.wb._changed();
      this.wb._parseEntry(this.entry,"fields",this.structureEditor.value,this.fieldsBase,value=>{this.fieldsBase=structuredClone(value);});
    }
    _source(){
      this.source=node("details","patrol-card-source");this.sourceTitle=node("summary");const info=node("pre");
      this.source.append(this.sourceTitle,info);this.node.append(this.source);
      this.source.ontoggle=()=>{if(this.source.open)info.textContent=JSON.stringify({reference:this.entry.source_ref,source_item:this.entry.source_item_id,group:this.entry.source_group,hash:this.entry.source_hash,edited_from:this.entry.edited_from,status:this.wb.sourceStatus?.[this.entry.source_ref]},null,2);};
    }
    sync(index){
      this.title.textContent=global.FocusPatrolUI.kindLabel(this.kind);
      this.role.hidden=!["message","authored_instruction","task_contract","selected_context"].includes(this.kind);
      if(this.role.value!==(this.entry.payload.role || "user"))this.role.value=this.entry.payload.role || "user";
      this.number.textContent=String(index+1).padStart(2,"0");for(const binding of this.bindings.values())binding.sync();
      this.node.classList.toggle("is-insertion-anchor",this.wb.interactions.anchor===this.entry.entry_id);
      if(this.payloadEditor && this.renderedPayload!==this.entry.payload && !this.entry.payload_error && !this.wb.composing){
        const raw=JSON.stringify(this.entry.payload,null,2),selection=this.payloadEditor.hasFocus?[this.payloadEditor.selectionStart,this.payloadEditor.selectionEnd]:null;
        this.payloadEditor.value=raw;this.renderedPayload=this.entry.payload;if(selection)this.payloadEditor.setSelectionRange(Math.min(selection[0],raw.length),Math.min(selection[1],raw.length));
      }
      const health=this.wb.sourceStatus?.[this.entry.source_ref],label=this.entry.source_label || "冻结来源";
      const status=health?.status==="unavailable"?"来源不可用 · 冻结内容保留":health?.status==="unknown"?"旧引用状态无法检查":this.wb.sourceFailed?"暂时无法检查来源":this.entry.edited_from?"已改写":"冻结引用";
      this.source.hidden=!this.entry.source_ref;this.sourceTitle.textContent=label+" · "+status;
      this.error.textContent=this.entry.fields_error || this.entry.content_error || this.entry.payload_error || "";this.error.hidden=!this.error.textContent;
    }
    fieldEditor(key){const binding=this.bindings.get(key);return key==="payload"?this.payloadEditor:binding?.port || binding?.input;}
    refresh(){for(const binding of this.bindings.values())binding.refresh();this.structureEditor?.refresh();this.payloadEditor?.refresh();}
    destroy(){for(const binding of this.bindings.values())binding.destroy();this.structureEditor?.destroy();this.payloadEditor?.destroy();}
  }
  global.FocusPatrolCards={create:(wb,entry)=>new ItemCard(wb,entry)};
})(window);
