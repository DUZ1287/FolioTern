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
      if(!state.running){if(state.error)throw new Error(state.error);location.href='/';return;}
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
