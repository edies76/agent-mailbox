'use strict';
const el = id => document.getElementById(id);
const make = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
const labels={queued:'En cola',in_progress:'En análisis',completed:'Respondido',blocked:'Bloqueado',cancelled:'Cancelado'};
const state={token:sessionStorage.getItem('mailbox-instance-token')||'',me:null,agents:[],box:'inbox',rows:[],cursor:0,more:false,selected:null,timer:null,busy:false,reloadQueued:false,readerGeneration:0};
let imageURL=null;
const retryKeys=new WeakMap();
function retryBody(form,body){const payload=JSON.stringify(body), previous=retryKeys.get(form);const key=previous?.payload===payload?previous.key:crypto.randomUUID();retryKeys.set(form,{payload,key});return {...body,idempotency_key:key};}
const date = value => new Date(value*1000).toLocaleString('es-CO',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const name = id => {const a=state.agents.find(v=>v.id===id); return a ? `${a.instance_name||a.name} · ${a.kind}` : id;};
async function api(path,method='GET',body) {
  const r=await fetch(path,{method,headers:{Authorization:`Bearer ${state.token}`,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,cache:'no-store'});
  const data=await r.json(); if(!r.ok) throw new Error(typeof data.detail==='string'?data.detail:`HTTP ${r.status}`); return data;
}
function fail(error){el('workspace-error').textContent=error.message||String(error);}
function clearError(){el('workspace-error').textContent='';}
async function connect(token){
  state.token=token.trim(); el('login-error').textContent='';
  try {
    const summary=await api('/inbox/summary'); state.me=summary.instance;
    const directory=await api('/agents?limit=200'); state.agents=directory.agents;
    el('login').hidden=true; el('agent-token').value='';
    if(el('remember').checked)sessionStorage.setItem('mailbox-instance-token',state.token); else sessionStorage.removeItem('mailbox-instance-token');
    el('my-name').textContent=state.me.instance_name; el('my-id').textContent=state.me.id;
    el('create-profile').hidden=!!state.me.profile_id;
    showSummary(summary); await loadMail(true); clearInterval(state.timer);
    state.timer=setInterval(async()=>{if(!state.busy){try{const summary=await api('/inbox/summary');const changed=Number(el('pending-count').textContent)!==summary.pending;showSummary(summary);if(changed&&state.box==='inbox')await loadMail(true);}catch(error){fail(error);}}},15000);
  }catch(error){state.token='';sessionStorage.removeItem('mailbox-instance-token');el('login').hidden=false;el('login-error').textContent=error.message;}
}
function showSummary(summary){
  el('pending-count').textContent=summary.pending;el('connection-state').textContent=`${summary.pending} pendientes · revisado ${new Date().toLocaleTimeString('es-CO',{hour:'2-digit',minute:'2-digit'})}`;
}
async function loadMail(reset){
  if(state.busy){state.reloadQueued=true;return;}state.busy=true;const box=state.box;clearError();el('refresh').disabled=true;
  try {
    if(state.box==='profile'){await showProfile();return;}
    const q=new URLSearchParams({limit:'50',after_id:reset?'0':String(state.cursor)});
    if(state.box==='inbox')q.set('unread_only','false');
    if(el('status-filter').value)q.set('status',el('status-filter').value);
    if(el('file-filter').value)q.set('has_attachments',el('file-filter').value);
    const result=await api(`/${box}?${q}`);if(state.box!==box)return;state.rows=reset?result.messages:[...state.rows,...result.messages];
    state.cursor=result.next_after_id;state.more=result.has_more;renderList();
    el('load-more').hidden=!state.more;
  }catch(error){fail(error);}finally{state.busy=false;el('refresh').disabled=false;if(state.reloadQueued){state.reloadQueued=false;loadMail(true);}}
}
function renderList(){
  if(state.box==='profile')return;
  const list=el('mail-list');list.replaceChildren();const search=el('search').value.trim().toLowerCase();
  const rows=state.rows.filter(r=>`${r.subject} ${r.text} ${name(r.from_agent_id)} ${name(r.to_agent_id)}`.toLowerCase().includes(search));
  el('list-count').textContent=`${rows.length} mensajes · más antiguo primero`;
  if(!rows.length){list.append(make('div','empty',state.rows.length?'Ningún mensaje cargado coincide con la búsqueda.':'No hay mensajes con estos filtros. Una solicitud nueva llegará solo a esta instancia.'));return;}
  for(const row of rows){
    const button=make('button',`mail-item${state.selected?.id===row.id?' selected':''}`);button.type='button';
    const route=make('div','mail-route');route.append(make('span','',name(state.box==='inbox'?row.from_agent_id:row.to_agent_id)),make('time','',date(row.created_at)));route.title=row.created_at_iso;
    button.append(route,make('h3','',row.subject||'Sin asunto'),make('div','mail-snippet',row.text));
    const badges=make('div','mail-badges');badges.append(make('span',`pill ${row.status}`,row.message_type==='reply'?'Respuesta':row.message_type==='note'?'Nota':labels[row.status]||row.status));
    if(row.attachment_count)badges.append(make('span','file-count',`${row.has_images?'▧ ':''}${row.attachment_count} adjunto${row.attachment_count===1?'':'s'}`));
    badges.append(make('span','file-count',`hilo ${row.thread_id}`));button.append(badges);
    button.addEventListener('click',()=>openMessage(row.id));list.append(button);
  }
}
async function openMessage(id){
  const generation=++state.readerGeneration, token=state.token;
  clearError();try{
    const row=await api(`/messages/${id}`);if(generation!==state.readerGeneration||token!==state.token)return;state.selected=row;renderList();
    const reader=el('reader');reader.replaceChildren();el('workspace').classList.add('reading');
    const back=make('button','text-button reader-back','← Volver a mensajes');back.style.display='none';back.onclick=()=>el('workspace').classList.remove('reading');reader.append(back);
    const meta=make('div','reader-meta');meta.append(make('span','eyebrow',`Solicitud ${row.id} / hilo ${row.thread_id}`),make('span',`pill ${row.status}`,labels[row.status]));reader.append(meta,make('h2','',row.subject||'Sin asunto'));
    const routing=make('div','reader-route');routing.append(make('div','',`De: ${name(row.from_agent_id)}`),make('div','',`Para: ${name(row.to_agent_id)}`),make('div','',`${date(row.created_at)} · ${row.created_at_iso}`));reader.append(routing,make('div','reader-body',row.text));
    if(row.status_note)reader.append(make('p','banner',row.status_note));
    if(row.attachments.length){const files=make('div','attachments');for(const file of row.attachments){
      const button=make('button','attachment');button.type='button';const info=make('div');info.append(make('b','',file.filename),make('span','',`${file.media_type} · ${(file.size_bytes/1024).toFixed(1)} KiB`));button.append(make('span','',file.media_type.startsWith('image/')?'▧':'↧'),info);
      button.onclick=()=>download(row.id,file);files.append(button);}reader.append(files);}
    const thread=await api(`/threads/${id}?limit=200`);
    if(state.selected?.id!==id||generation!==state.readerGeneration)return;
    for(const reply of thread.messages.filter(m=>m.id!==id)){const item=make('article','thread-entry');item.append(make('span','eyebrow',`${name(reply.from_agent_id)} · ${date(reply.created_at)} · #${reply.id}`),make('pre','',reply.text));reader.append(item);}
    if(thread.has_more)reader.append(make('p','hint small','El hilo continúa. Usa /threads con next_after_id para consultar el resto.'));
    if(row.to_agent_id===state.me.id&&!['completed','cancelled'].includes(row.status))appendReply(reader,row);
    if(row.to_agent_id===state.me.id&&row.acknowledged_at===null&&['reply','note'].includes(row.message_type)){
      const ack=make('button','button small secondary','Marcar resultado como procesado');
      ack.onclick=async()=>{try{await api(`/inbox/${row.id}/ack`,'POST',{});showSummary(await api('/inbox/summary'));await loadMail(true);if(state.selected?.id===row.id)await openMessage(row.id);}catch(error){fail(error);}};
      reader.append(ack);
    }
    const remove=make('button','text-button small','Eliminar mensaje');remove.dataset.confirm='no';remove.onclick=async()=>{
      if(remove.dataset.confirm==='no'){remove.dataset.confirm='yes';remove.textContent='Confirmar: borrar para ambos y sus adjuntos';return;}
      try{await api(`/messages/${row.id}`,'DELETE');state.selected=null;reader.replaceChildren(make('div','empty','Mensaje eliminado. Sus respuestas permanecen con el hilo original.'));await loadMail(true);}catch(error){fail(error);}
    };reader.append(remove);
  }catch(error){fail(error);}
}
function appendReply(reader,row){
  const form=make('form','reply-box');form.innerHTML='<h3>Responder a esta solicitud</h3><p class="small muted">El servidor fija la instancia y el hilo. La respuesta final cierra solo esta solicitud.</p><label>Hallazgos y evidencia<textarea name="answer" required placeholder="Qué investigaste, resultados, referencias y lo que queda sin confirmar."></textarea></label><label>Adjuntos de la respuesta<input type="file" name="files" multiple></label><div class="reply-actions"><select name="status" aria-label="Tipo de respuesta"><option value="completed">Respuesta final</option><option value="in_progress">Avance · sigue pendiente</option><option value="blocked">Bloqueado · sigue pendiente</option></select><button class="button small accent">Enviar al solicitante →</button></div><p class="status" role="status"></p>';
  const output=form.querySelector('.status');form.addEventListener('submit',async event=>{
    event.preventDefault();const submit=form.querySelector('button');submit.disabled=true;output.textContent='Enviando respuesta al hilo correcto…';
    try{
      const current=await api(`/messages/${row.id}`);if(current.request_fingerprint!==row.request_fingerprint)throw new Error('La solicitud cambió; vuelve a leerla.');
      const attachments=await encodeFiles(form.elements.files.files);
      await api(`/messages/${row.id}/reply`,'POST',retryBody(form,{request_fingerprint:row.request_fingerprint,text:form.elements.answer.value,status:form.elements.status.value,attachments}));
      output.textContent='Respuesta enviada a la instancia solicitante.';showSummary(await api('/inbox/summary'));await loadMail(true);if(state.selected?.id===row.id)await openMessage(row.id);
    }catch(error){output.className='status error';output.textContent=error.message;}finally{submit.disabled=false;}
  });reader.append(form);
  const progress=make('button','text-button small','Marcar en análisis');progress.onclick=async()=>{try{await api(`/messages/${row.id}/status`,'PATCH',{status:'in_progress',note:'Investigando la solicitud'});await openMessage(row.id);await loadMail(true);}catch(error){fail(error);}};reader.append(progress);
}
async function encodeFiles(files){
  const list=[...files];if(list.length>3||list.reduce((n,f)=>n+f.size,0)>10*1024*1024)throw new Error('Máximo 3 archivos y 10 MiB en total.');
  return Promise.all(list.map(file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({filename:file.name,media_type:file.type||'application/octet-stream',data_base64:String(reader.result).split(',')[1]});reader.onerror=()=>reject(new Error('No se pudo leer el archivo.'));reader.readAsDataURL(file);})));
}
async function download(id,file){
  try{const r=await fetch(`/inbox/${id}/attachments/${file.index}/download`,{headers:{Authorization:`Bearer ${state.token}`}});if(!r.ok)throw new Error('No se pudo descargar el adjunto.');const blob=await r.blob();const url=URL.createObjectURL(blob);
    if(['image/png','image/jpeg','image/webp','image/gif'].includes(file.media_type)){if(imageURL)URL.revokeObjectURL(imageURL);imageURL=url;el('preview-image').src=url;el('preview-image').alt=file.filename;el('image-dialog').showModal();}
    else{const a=make('a');a.href=url;a.download=file.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  }catch(error){fail(error);}
}
async function showProfile(){
  el('load-more').hidden=true;el('mail-list').replaceChildren();const reader=el('reader');reader.replaceChildren();el('workspace').classList.add('reading');
  if(!state.me.profile_id){reader.append(make('h2','','Esta instancia aún no tiene perfil.'),make('p','reader-body','Puedes seguir enviando y recibiendo mensajes. Crea un perfil para reunir varios chats con inboxes independientes.'));return;}
  const data=await api(`/profiles/${state.me.profile_id}/activity`);el('list-count').textContent=`${data.instances.length} instancias · ${data.pending_total} pendientes`;
  reader.append(make('span','eyebrow','Actividad compartida · sin contenido privado'),make('h2','','Un perfil. Buzones separados.'),make('p','banner','Solo se muestran conteos y asuntos que el remitente eligió compartir. Los cuerpos y archivos de otros chats no son accesibles.'));
  for(const instance of data.instances){const article=make('article','profile-instance');article.append(make('h3','',instance.instance_name),make('code','',instance.instance_id),make('p','',`${instance.pending} pendientes / ${instance.total} mensajes recibidos`));
    for(const shared of instance.shared_subjects)article.append(make('p','',`${shared.subject} · ${shared.created_at_iso}`));reader.append(article);
    const preview=make('div','mail-item');preview.append(make('h3','',instance.instance_name),make('span','pill',`${instance.pending} pendientes`));el('mail-list').append(preview);
  }
}
for(const button of document.querySelectorAll('[data-box]'))button.addEventListener('click',()=>{state.box=button.dataset.box;state.selected=null;document.querySelectorAll('[data-box]').forEach(n=>n.classList.toggle('active',n===button));el('box-title').textContent={inbox:'Recibidos',outbox:'Enviados',profile:'Actividad del perfil'}[state.box];el('box-subtitle').textContent=state.box==='profile'?'Conteos del grupo, sin abrir otros buzones.':'Una conversación por solicitud.';el('workspace').classList.remove('reading');loadMail(true);});
for(const id of ['status-filter','file-filter'])el(id).addEventListener('change',()=>loadMail(true));
el('search').addEventListener('input',renderList);el('load-more').onclick=()=>loadMail(false);el('refresh').onclick=async()=>{await loadMail(true);try{showSummary(await api('/inbox/summary'));}catch(error){fail(error);}};
el('compose').onclick=async()=>{try{const directory=await api('/agents?limit=200');state.agents=directory.agents;}catch(error){fail(error);return;}el('recipient').replaceChildren();for(const a of state.agents.filter(a=>a.id!==state.me.id)){const option=make('option','',`${a.instance_name||a.name} · ${a.kind} · ${a.project||'sin proyecto'} · ${a.id.slice(0,8)}`);option.value=a.id;el('recipient').append(option);}el('compose-status').textContent='';el('compose-dialog').showModal();};
el('compose-form').addEventListener('submit',async event=>{event.preventDefault();el('send-request').disabled=true;el('compose-status').textContent='Enviando…';try{const attachments=await encodeFiles(el('request-files').files);const text=`## Solicitud\n${el('task').value}\n\n## Resultado esperado\n${el('expected-output').value}`;await api('/messages','POST',retryBody(el('compose-form'),{to_instance_id:el('recipient').value,subject:el('subject').value,text,attachments,share_subject:el('share-subject').checked}));retryKeys.delete(el('compose-form'));el('compose-form').reset();el('compose-dialog').close();if(state.box==='outbox')await loadMail(true);}catch(error){el('compose-status').textContent=error.message;}finally{el('send-request').disabled=false;}});
el('create-profile').onclick=()=>{el('profile-form').hidden=false;el('profile-result').hidden=true;el('profile-status').textContent='';el('profile-name').value=state.me.kind;el('profile-dialog').showModal();};
el('profile-form').addEventListener('submit',async event=>{event.preventDefault();try{const result=await api('/profiles','POST',{name:el('profile-name').value,kind:state.me.kind,project:state.me.project});state.me=result.instance;el('create-profile').hidden=true;el('profile-form').hidden=true;el('profile-result').hidden=false;el('profile-config').value=JSON.stringify({url:location.origin,profile_id:result.profile.id,enrollment_token:result.enrollment_token,instance_prefix:result.profile.name,auto_continue_on_stop:true},null,2);}catch(error){el('profile-status').textContent=error.message;}});
el('save-profile').onclick=()=>{const url=URL.createObjectURL(new Blob([el('profile-config').value],{type:'application/json'}));const a=make('a');a.href=url;a.download='mailbox-profile-private.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
for(const button of document.querySelectorAll('[data-close]'))button.onclick=()=>el(button.dataset.close).close();
el('login-form').addEventListener('submit',event=>{event.preventDefault();connect(el('agent-token').value);});
el('login-config').addEventListener('change',async event=>{try{const cfg=JSON.parse(await event.target.files[0].text());if(new URL(cfg.url).origin!==location.origin)throw new Error('El JSON pertenece a otro servidor. Abre su URL.');el('agent-token').value=cfg.token||'';}catch(error){el('login-error').textContent=error.message;}});
el('logout').onclick=()=>{clearInterval(state.timer);state.token='';state.me=null;state.rows=[];state.selected=null;state.readerGeneration++;el('profile-config').value='';for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();sessionStorage.removeItem('mailbox-instance-token');el('reader').replaceChildren();el('mail-list').replaceChildren();el('login').hidden=false;};
fetch('/health').then(r=>r.json()).then(v=>document.querySelectorAll('[data-version]').forEach(n=>n.textContent=`v${v.version}`)).catch(()=>{});
if(state.token){el('remember').checked=true;connect(state.token);}
