const csrf = document.querySelector('meta[name="csrf-token"]').content;
const notice = document.getElementById('notice');
function tell(message, error=false){notice.hidden=false;notice.textContent=message;notice.className=error?'error':'';}
async function post(path, body={}){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Radar-CSRF':csrf},body:JSON.stringify(body)});
  const value=await response.json();
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'请求失败，请检查输入或刷新页面。');
  return value;
}
const refresh=document.getElementById('refresh');
if(refresh)refresh.addEventListener('click',async()=>{
  refresh.disabled=true;tell('正在采集公开资料并生成日报，可以稍后返回查看。');
  try{
    await post('/api/run');
    for(let i=0;i<360;i++){
      await new Promise(resolve=>setTimeout(resolve,2000));
      const response=await fetch('/api/status');const state=await response.json();
      if(!state.running){if(state.error)throw new Error(state.error);location.href='/today';return;}
    }
    tell('任务仍在运行，请稍后刷新页面查看。');
  }catch(error){tell(error.message,true);}finally{refresh.disabled=false;}
});
document.querySelectorAll('.feedback-actions').forEach(group=>{
  group.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',async()=>{
    button.disabled=true;
    try{await post('/api/feedback',{item_id:group.dataset.item,action:button.dataset.action});group.querySelectorAll('button').forEach(b=>b.classList.remove('selected'));button.classList.add('selected');const state=group.querySelector('.feedback-state');if(state)state.textContent='已保存';else tell('反馈已保存。');}
    catch(error){tell(error.message,true);}finally{button.disabled=false;}
  }));
});
const form=document.getElementById('settings-form');
if(form)form.addEventListener('submit',async(event)=>{
  event.preventDefault();const submit=form.querySelector('button[type=submit]');submit.disabled=true;
  try{const values=Object.fromEntries(new FormData(form));await post('/api/settings',values);form.elements.api_key.value='';tell('设置已保存到本机。可以测试连接，或返回首页更新日报。');}
  catch(error){tell(error.message,true);}finally{submit.disabled=false;}
});
const test=document.getElementById('test-model');
if(test)test.addEventListener('click',async()=>{
  test.disabled=true;tell('正在测试已保存的模型配置…');
  try{const result=await post('/api/test-model');tell(result.message);}
  catch(error){tell(error.message,true);}finally{test.disabled=false;}
});

// Research data is rendered with textContent, never interpreted as HTML.
function node(tag, cls, text){const value=document.createElement(tag);if(cls)value.className=cls;if(text!==undefined)value.textContent=text;return value;}
function safeLink(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)&&!u.username?u.href:'#';}catch{return '#';}}
const toolNames={search_projects:'搜索公开项目',read_project:'阅读项目说明',read_releases:'核对发布记录',search_papers:'查找论文摘要',search_history:'查询本地历史',finish_research:'形成研究结论'};
async function waitForResearch(){
  for(let i=0;i<30;i++){
    const response=await fetch('/api/status');const state=await response.json();
    if(state.error)throw new Error(state.error);
    if(state.task_id){location.href='/research/'+encodeURIComponent(state.task_id);return;}
    if(!state.running)throw new Error('任务未启动，请重试。');
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  throw new Error('任务启动较慢，请刷新工作台查看最近研究。');
}
const researchForm=document.getElementById('research-form');
if(researchForm){
  const question=document.getElementById('research-question');
  const count=()=>document.getElementById('question-length').textContent=question.value.length+' / 2000';
  question.addEventListener('input',count);
  document.querySelectorAll('[data-question]').forEach(button=>button.addEventListener('click',()=>{question.value=button.dataset.question;count();question.focus();}));
  researchForm.addEventListener('submit',async event=>{
    event.preventDefault();const button=document.getElementById('start-research');button.disabled=true;tell('正在创建研究任务…');
    try{await post('/api/research',{question:question.value.trim()});await waitForResearch();}
    catch(error){tell(error.message,true);}finally{button.disabled=false;}
  });
}
const detail=document.getElementById('research-detail');
if(detail){
  const taskId=detail.dataset.taskId;
  const resultBox=document.getElementById('research-result'),traceBox=document.getElementById('research-trace'),evidenceBox=document.getElementById('research-evidence');
  const pauseButton=document.getElementById('pause-research'),resumeButton=document.getElementById('resume-research');
  let lastVersion='',refreshTimer=null;
  function drawTask(task){
    const badge=document.getElementById('task-badge');
    badge.textContent=task.status==='completed'?(task.result?.outcome==='insufficient'?'已结束 · 证据不足':'研究完成'):task.active?'正在研究':'已暂停 / 可恢复';
    badge.className='research-badge '+(task.active?'running':task.status);
    pauseButton.hidden=!task.active;resumeButton.hidden=task.active||task.status==='completed';
    const usage=task.usage||{};document.getElementById('task-usage').textContent=`${task.evidence.length} 份证据 · ${usage.calls||0} 次已记录模型调用`;
    document.querySelectorAll('#research-stages span').forEach((element,index)=>element.classList.toggle('done',index===0||(index===1&&task.trace.some(e=>e.type==='tool_call'))||(index===2&&task.evidence.length>0)||(index===3&&task.status==='completed')));
    resultBox.replaceChildren();
    if(task.result){
      resultBox.append(node('p','result-lead',task.result.summary));
      for(const finding of task.result.findings){const block=node('article','finding');block.append(node('p','',finding.claim));const refs=node('div','evidence-refs');for(const id of finding.evidence_ids){const a=node('a','evidence-chip',id);a.href='#evidence-'+id;refs.append(a);}block.append(refs);resultBox.append(block);}
      for(const [title,values] of [['实践建议',task.result.recommendations],['证据与限制',task.result.limitations]]){resultBox.append(node('h3','',title));const list=node('ul','result-list');values.forEach(value=>list.append(node('li','',value)));resultBox.append(list);}
    }else{
      const placeholder=node('div','research-wait');placeholder.append(node('span',task.active?'working-signal':'','◎'),node('h3','',task.active?'Agent 正在沿着证据继续研究。':'研究进度已保存。'),node('p','muted',task.error||'工具执行和证据会实时更新。资料足够后，结论会出现在这里。'));resultBox.append(placeholder);
    }
    document.getElementById('evidence-count').textContent=task.evidence.length+' 份资料';evidenceBox.replaceChildren();
    for(const evidence of task.evidence){const card=node('details','evidence-card');card.id='evidence-'+evidence.id;const summary=node('summary');summary.append(node('span','evidence-chip',evidence.id),node('span','',evidence.title));card.append(summary);const source=node('a','source-link','打开原始来源 ↗');source.href=safeLink(evidence.url);source.target='_blank';source.rel='noopener noreferrer';card.append(node('p','fine',({readme:'项目方自述 · 未运行验证',release:'官方发布说明',abstract:'仅阅读论文摘要',discovery:'发现线索 · 非质量证明',local_history:'本地历史记录'})[evidence.kind]||evidence.kind),source,node('p','evidence-excerpt',evidence.text));evidenceBox.append(card);}
    traceBox.replaceChildren();
    for(const event of task.trace){const row=node('li','trace-event '+(event.status==='error'?'error':''));let title={tool_call:'调用工具',tool_result:'收到资料',finished:'形成结论',paused:'保存进度',model_error:'模型请求失败',validation_error:'修正参数或引用',invalid_output:'修正输出格式'}[event.type]||event.type;row.append(node('span','trace-dot'),node('strong','',toolNames[event.tool]||title));row.append(node('p','',event.type==='tool_result'?(event.status==='ok'?`返回 ${event.evidence_ids?.length||0} 份证据`:'未取得资料，尝试其他路径'):title));if(event.arguments){row.append(node('p','trace-arguments',event.arguments.query||event.arguments.repo||''));}if(event.error)row.append(node('p','fine',event.error));row.append(node('time','',new Date(event.at).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})));traceBox.append(row);}
    if(!task.trace.length)traceBox.append(node('li','muted','等待第一步工具调用…'));
  }
  async function refreshTask(){
    try{const response=await fetch('/api/research/'+taskId);if(!response.ok)throw new Error('读取研究任务失败');const task=await response.json();const version=task.updated_at+':'+task.active;if(version!==lastVersion){drawTask(task);lastVersion=version;}if(task.active)refreshTimer=setTimeout(refreshTask,1500);}
    catch(error){tell(error.message,true);}
  }
  pauseButton.addEventListener('click',async()=>{pauseButton.disabled=true;try{const result=await post('/api/research/'+taskId+'/pause');tell(result.message);}catch(error){tell(error.message,true);}finally{pauseButton.disabled=false;}});
  resumeButton.addEventListener('click',async()=>{resumeButton.disabled=true;try{await post('/api/research/'+taskId+'/resume');tell('正在使用已有证据恢复研究…');lastVersion='';clearTimeout(refreshTimer);await refreshTask();}catch(error){tell(error.message,true);}finally{resumeButton.disabled=false;}});
  refreshTask();
}
