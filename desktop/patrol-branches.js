/* 本文件对外提供 mount/get 的小兵精确分支控制。
 * 输入为 agent、目录 API 和启动回调，输出为 restart/continue/resume 的明确执行选择。
 * 工作流冻结所选 Run/C，读取异步目录时保留选择，resume 提交原中断响应，不重新投放。
 * 示例：FocusPatrolBranches.get(agentId) 返回 {run_id,checkpoint_id}。
 */
(function (global) {
  const selections = new Map();
  const resumePayloads = new Map();
  global.FocusPatrolBranches = {
    get(id) { return selections.get(id) || {}; },
    async mount(root, { agent, api, onRun, onDraft, onError }) {
      if (!root || !agent) return;
      root.dataset.agentId=agent.agent_id;
      const select = document.createElement('select'); select.setAttribute('aria-label','执行分支');
      const checkpoint = document.createElement('input'); checkpoint.setAttribute('aria-label','精确 checkpoint'); checkpoint.placeholder='选择分支 checkpoint，可输入旧版本';
      const latest = agent.latest_run;
      const selected = selections.get(agent.agent_id) || { run_id:latest?.run_id, checkpoint_id:latest?.final_checkpoint_id };
      checkpoint.value = selected.checkpoint_id || '';
      const set = () => selections.set(agent.agent_id, { run_id:select.value || selected.run_id, checkpoint_id:checkpoint.value || null });
      checkpoint.oninput = set;
      root.append(select, checkpoint);
      const edit=document.createElement('button'); edit.type='button'; edit.textContent='编辑原定义并新建分支';
      edit.onclick=async()=>{edit.disabled=true;try{const draft=await api(`/desktop/api/agents/${agent.agent_id}/drafts/open`,{method:'POST'});onDraft(draft);}catch(error){onError(error);}finally{edit.disabled=false;}};
      root.append(edit);
      const audit=document.createElement('details'),auditTitle=document.createElement('summary'); auditTitle.textContent='查看冻结定义、来源与执行审计';audit.append(auditTitle);root.append(audit);
      let auditGeneration=0;
      audit.addEventListener('toggle',async()=>{if(!audit.open)return;const generation=++auditGeneration;try{const value=await api(`/desktop/api/runs/${select.value || selected.run_id}/patrol-definition`);if(generation!==auditGeneration||!root.isConnected)return;const body=document.createElement('pre');body.textContent=JSON.stringify(value,null,2);audit.replaceChildren(auditTitle,body);}catch(error){onError(error);}});
      try {
        const catalog = await api('/desktop/api/patrol/sources'); if (!root.isConnected) return;
        const branches = catalog.branches.filter(b => b.agent_id === agent.agent_id);
        for (const branch of branches) { const option=document.createElement('option'); option.value=branch.run_id; option.textContent=`${branch.status} · ${branch.run_id.slice(0,8)}`; select.append(option); }
        select.value=selected.run_id || branches[0]?.run_id || '';
        const payload=document.createElement('textarea'); payload.placeholder='原中断响应 JSON'; payload.setAttribute('aria-label','中断响应 JSON');
        const resume=document.createElement('button'); resume.type='button'; resume.textContent='恢复所选原中断';
        const refresh=()=>{const branch=branches.find(b=>b.run_id===select.value);payload.value=resumePayloads.get(select.value)||'{}';payload.hidden=resume.hidden=branch?.status!=='interrupted';};
        payload.oninput=()=>resumePayloads.set(select.value,payload.value);
        select.onchange=()=>{checkpoint.value=branches.find(b=>b.run_id===select.value)?.checkpoint_id || '';set();auditGeneration++;audit.open=false;refresh();}; set();refresh();
        resume.onclick=async()=>{resume.disabled=true;try{const run=await api(`/desktop/api/runs/${select.value}/patrol-resume`,{method:'POST',body:JSON.stringify({resume:JSON.parse(payload.value)})});onRun(run);}catch(error){onError(error);}finally{resume.disabled=false;}};
        root.append(payload,resume);
      } catch (error) { onError(error); }
    },
  };
})(window);
