// Configuration screens only render untrusted values as text.
const currentPage=document.body.dataset.page;
async function api(path,method='GET',body){
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json','X-Radar-CSRF':csrf},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const value=await r.json();if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:'请求失败，请检查输入。');return value;
}
function actionButton(label,handler,cls='secondary'){
  const b=node('button',cls,label);b.type='button';b.onclick=async()=>{b.disabled=true;try{await handler();}catch(e){tell(e.message,true);}finally{b.disabled=false;}};return b;
}
async function watchJob(reload){
  tell('正在执行实际请求，请稍候…');
  for(let i=0;i<180;i++){
    await new Promise(r=>setTimeout(r,1000));const s=await api('/api/status');
    if(!s.running){await reload();if(s.error)throw new Error(s.error);tell('请求已结束，具体结果见对应档案。');return;}
  }tell('请求仍在运行，可稍后刷新状态。');
}
function fillForm(f,data){for(const [k,v] of Object.entries(data)){const e=f.elements.namedItem(k);if(!e)continue;if(e.type==='checkbox')e.checked=Boolean(v);else e.value=Array.isArray(v)?v.join(', '):v??'';}}
function closeDialogs(){document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.close).close());}
closeDialogs();
const typeLabels={github_releases:'GitHub Releases',github_search:'GitHub 搜索',arxiv_category:'arXiv 分类',arxiv_search:'arXiv 搜索',rss:'RSS / Atom'};
const statusLabel={ok:'完成',partial:'部分完成',error:'失败'};
let sourceRows=[],sourceEditing=null,profileRows=[],profileEditing=null;
function sourceDialog(row){
  const f=document.getElementById('source-form');f.reset();sourceEditing=row?.id??null;
  if(row)fillForm(f,row);f.querySelector('.dialog-error').textContent='';sourceHint();document.getElementById('source-dialog').showModal();
}
function sourceHint(){const k=document.getElementById('source-form').elements.kind.value;document.getElementById('source-hint').textContent={github_releases:'填写 owner/repository，例如 langchain-ai/langgraph。',github_search:'例如 agent language:Python created:>{date}；按回看日期搜索 Python 项目。',arxiv_category:'例如 cs.AI、cs.CL 或 cs.LG。',arxiv_search:'使用 arXiv 查询语法，例如 all:agent AND cat:cs.AI。',rss:'仅支持公网 HTTPS、标准端口；私网、DNS 指向内网与重定向均被阻止。'}[k];}
async function loadSources(){sourceRows=await api('/api/sources');renderSources();}
function renderSources(){
  const list=document.getElementById('source-list');list.replaceChildren();const term=document.getElementById('source-filter').value.toLowerCase();
  const rows=sourceRows.filter(x=>(x.name+' '+x.target+' '+typeLabels[x.kind]).toLowerCase().includes(term));
  document.getElementById('source-count').textContent=`${sourceRows.length} 个来源 · ${sourceRows.filter(x=>x.enabled).length} 个已启用`;
  if(!rows.length)list.append(node('div','config-empty','暂无匹配来源。添加一个你想持续关注的信息入口。'));
  for(const x of rows){
    const card=node('article','config-card'),head=node('div','config-head');head.append(node('h3','',x.name),node('span','pill'+(x.enabled?'':' off'),x.enabled?'已启用':'已停用'));card.append(head,node('div','config-target',x.target));
    const meta=node('div','config-meta');meta.append(node('span','',typeLabels[x.kind]),node('span','',`单次 ≤ ${x.limit} 条`),node('span','',x.keywords.length?'关键词：'+x.keywords.join(' / '):'不限制关键词'));card.append(meta);
    const actions=node('div','config-actions');actions.append(actionButton('编辑',()=>sourceDialog(x)),actionButton(x.enabled?'停用':'启用',async()=>{const {id,last_result,...v}=x;await api('/api/sources/'+id,'PUT',{...v,enabled:!v.enabled});await loadSources();}),actionButton('测试连接',async()=>{await post('/api/sources/'+x.id+'/test');await watchJob(loadSources);}),actionButton('采集到知识库',async()=>{await post('/api/sources/'+x.id+'/collect');await watchJob(loadSources);}),actionButton('删除',async()=>{if(confirm('删除此信息源？历史采集数据会保留。')){await api('/api/sources/'+x.id,'DELETE');await loadSources();}},'secondary danger'));card.append(actions);
    const r=x.last_result,detail=node('div','config-result');
    if(r){detail.append(node('p','',`${r.action==='test'?'连接测试':'最近采集'} · ${statusLabel[r.status]||r.status} · ${r.count} 条 · ${new Date(r.checked_at).toLocaleString('zh-CN')}`));if(r.error)detail.append(node('p','config-error',r.error));if(r.coverage_note)detail.append(node('p','',r.coverage_note));for(const item of r.preview||[]){const a=node('a','',item.title);a.href=safeLink(item.url);a.target='_blank';a.rel='noopener noreferrer';detail.append(a);}}
    else detail.append(node('p','','尚未检查，不代表连接成功。'));card.append(detail);list.append(card);
  }
}
if(currentPage==='sources'){
  document.getElementById('add-source').onclick=()=>sourceDialog();document.getElementById('source-filter').oninput=renderSources;
  const f=document.getElementById('source-form');f.elements.kind.onchange=sourceHint;
  f.onsubmit=async e=>{e.preventDefault();const b=f.querySelector('[type=submit]');b.disabled=true;try{const v=Object.fromEntries(new FormData(f));v.enabled=f.elements.enabled.checked;v.limit=Number(v.limit);v.keywords=v.keywords.split(/[,，]/).map(x=>x.trim()).filter(Boolean);await api('/api/sources'+(sourceEditing?'/'+sourceEditing:''),sourceEditing?'PUT':'POST',v);document.getElementById('source-dialog').close();await loadSources();tell('信息源已保存。');}catch(e){f.querySelector('.dialog-error').textContent=e.message;}finally{b.disabled=false;}};
  loadSources().catch(e=>tell(e.message,true));
}
function profileDialog(row){const f=document.getElementById('profile-form');f.reset();profileEditing=row?.id??null;if(row)fillForm(f,row);f.elements.api_key.value='';f.querySelector('.dialog-error').textContent='';document.getElementById('profile-dialog').showModal();}
async function loadProfiles(){
  const value=await api('/api/profiles');profileRows=value.profiles;const list=document.getElementById('profile-list');list.replaceChildren();
  if(!profileRows.length)list.append(node('div','config-empty','尚未创建模型档案。添加后可测试工具调用能力；未配置模型时，基础信息采集仍可使用。'));
  for(const x of profileRows){
    const card=node('article','config-card'),head=node('div','config-head');let active=[];if(value.active_research===x.id)active.push('研究');if(value.active_daily===x.id)active.push('日报');head.append(node('h3','',x.name),node('span','pill',!x.enabled?'已停用':active.length?'用于 '+active.join(' + '):'待启用为当前模型'));
    card.append(head,node('div','config-target',x.base_url+' / '+x.model),node('div','config-meta',`${x.provider} · 每日 ${x.max_llm_calls} 次 / ${x.max_token_budget.toLocaleString()} Token 预留 · 密钥已安全保存`));
    const actions=node('div','config-actions');actions.append(actionButton('编辑',()=>profileDialog(x)),actionButton('使用此模型',async()=>{await post('/api/profiles/'+x.id+'/activate');await loadProfiles();tell('模型已切换。已有研究需使用原配置恢复。');}),actionButton('完整能力测试',async()=>{await post('/api/profiles/'+x.id+'/test');await watchJob(loadProfiles);}),actionButton('读取模型列表',async()=>{const v=await post('/api/profiles/'+x.id+'/models');const dl=document.getElementById('available-models');dl.replaceChildren(...v.models.map(x=>{const o=document.createElement('option');o.value=x;return o;}));profileDialog(x);tell(`读取到 ${v.models.length} 个模型，在 Model ID 输入框中选择并保存。`);}),actionButton('删除',async()=>{if(confirm('删除该档案和保存的密钥？历史研究会保留。')){await api('/api/profiles/'+x.id,'DELETE');await loadProfiles();}},'secondary danger'));card.append(actions);
    const result=node('div','config-result');if(x.last_test){result.append(node('p','',`${x.last_test.ok?'完整能力测试通过':'能力测试未通过'} · ${new Date(x.last_test.checked_at).toLocaleString('zh-CN')}`));for(const stage of x.last_test.stages)result.append(node('p',stage.ok?'':'config-error',(stage.ok?'✓ ':'× ')+stage.name+'：'+stage.message));}else result.append(node('p','','尚未验证实际连接与工具调用能力。'));card.append(result);list.append(card);
  }
}
if(currentPage==='models'){
  document.getElementById('add-profile').onclick=()=>profileDialog();const f=document.getElementById('profile-form');
  f.onsubmit=async e=>{e.preventDefault();const b=f.querySelector('[type=submit]');b.disabled=true;try{const v=Object.fromEntries(new FormData(f));for(const k of ['enabled','json_mode'])v[k]=f.elements[k].checked;for(const k of ['max_llm_calls','max_token_budget','max_output_tokens'])v[k]=Number(v[k]);await api('/api/profiles'+(profileEditing?'/'+profileEditing:''),profileEditing?'PUT':'POST',v);f.elements.api_key.value='';document.getElementById('profile-dialog').close();await loadProfiles();tell('模型档案已安全保存。选择“使用此模型”即可生效。');}catch(e){f.querySelector('.dialog-error').textContent=e.message;}finally{b.disabled=false;}};
  loadProfiles().catch(e=>tell(e.message,true));
}
async function loadSystem(){
  const s=await api('/api/system');
  if(currentPage==='system'){fillForm(document.getElementById('appearance-form'),s.appearance);document.getElementById('data-path').textContent=s.data_path;}
  if(currentPage==='automation'){fillForm(document.getElementById('schedule-form'),s.schedule);document.getElementById('last-auto').textContent=s.last_auto_date?'窗口内最近尝试日期：'+s.last_auto_date:''; const run=s.last_external_run;document.getElementById('external-result').textContent=run?'最近后台运行 · '+new Date(run.at).toLocaleString('zh-CN')+' · '+run.message:'尚无后台采集记录。';}
  if(currentPage==='status'){
    document.getElementById('runtime-state').textContent=(s.state.running?'当前任务正在运行：'+s.state.kind:'当前无正在运行的任务。')+(s.state.error?' '+s.state.error:'');const list=document.getElementById('run-list');list.replaceChildren();
    for(const r of s.runs){const box=node('article','config-card');box.append(node('h3','',new Date(r.saved_at).toLocaleString('zh-CN')),node('p','config-meta',`保存 ${r.item_ids.length} 个版本`));for(const x of r.statuses)box.append(node('p',x.error?'config-error':'config-meta',`${x.name||x.source} · ${statusLabel[x.status]||x.status} · ${x.count} 条${x.error?' · '+x.error:''}`));list.append(box);}if(!s.runs.length)list.append(node('div','config-empty','还没有采集日志。信息源测试结果显示在信息源管理页。'));
  }
}
if(['system','automation','status'].includes(currentPage))loadSystem().catch(e=>tell(e.message,true));
document.getElementById('reload-status')?.addEventListener('click',()=>loadSystem().catch(e=>tell(e.message,true)));
document.getElementById('appearance-form')?.addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{const v={theme:f.elements.theme.value,reduce_motion:f.elements.reduce_motion.checked};await post('/api/appearance',v);document.body.dataset.theme=v.theme;document.body.dataset.motion=v.reduce_motion?'reduce':'normal';tell('外观已保存。');}catch(e){tell(e.message,true);}});
document.getElementById('schedule-form')?.addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{const v=await post('/api/schedule',{time:f.elements.time.value,enabled:f.elements.enabled.checked,background:f.elements.background.checked});tell(v.message);}catch(e){tell(e.message,true);}});

async function loadLibrary(){
  const q=new URLSearchParams({q:document.getElementById('library-query').value,saved:String(document.getElementById('library-saved').checked)});
  const rows=await api('/api/library?'+q);const list=document.getElementById('library-list');list.replaceChildren();
  if(!rows.length)list.append(node('div','config-empty','暂无匹配的已采集信息。可先在信息源管理中手动采集。'));
  for(const x of rows){const box=node('article','config-card'),title=node('a','',x.title);title.href=safeLink(x.url);title.target='_blank';title.rel='noopener noreferrer';const head=node('h3','');head.append(title);box.append(head,node('p','config-meta',x.source+' · '+(x.action?({saved:'已收藏',useful:'有用',hard:'暂时太难',ignore:'不感兴趣',completed:'实践已完成'}[x.action]):'未标记')));const actions=node('div','config-actions');for(const [action,label] of [['saved','收藏'],['useful','标记有用'],['completed','实践已完成']])actions.append(actionButton(label,async()=>{await post('/api/feedback',{item_id:x.id,action});await loadLibrary();}));box.append(actions);list.append(box);}
}
if(currentPage==='saved'){document.getElementById('library-search').onclick=()=>loadLibrary().catch(e=>tell(e.message,true));document.getElementById('library-saved').onchange=()=>loadLibrary().catch(e=>tell(e.message,true));document.getElementById('library-query').onkeydown=e=>{if(e.key==='Enter')loadLibrary().catch(e=>tell(e.message,true));};loadLibrary().catch(e=>tell(e.message,true));}
