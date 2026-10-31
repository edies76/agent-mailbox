(() => {
  const byId = id => document.getElementById(id);
  const state = {agents: [], messages: [], cursor: 0, humanId: null, busy: false, sending: false, blobs: [], directoryKey: '', draft: null, release: null, restore: null};
  const csrf = () => { const value = document.cookie.split('; ').find(x => x.startsWith('__Host-agent-mailbox-csrf=') || x.startsWith('agent-mailbox-csrf=')); return value ? decodeURIComponent(value.split('=').slice(1).join('=')) : ''; };
  async function api(path, method='GET', body) {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') headers['X-CSRF-Token'] = csrf();
    const response = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : undefined, credentials:'same-origin', cache:'no-store'});
    const data = await response.json();
    if (!response.ok) { if (response.status === 401) location.href='/dashboard'; const error=new Error(data.detail || `HTTP ${response.status}`);error.status=response.status;throw error; }
    return data;
  }
  function updateDirectory() {
    const select=byId('agent'), selected=select.value;
    byId('agent-count').textContent=`${state.agents.length} ${state.agents.length===1?'instancia':'instancias'}`;
    const key=JSON.stringify(state.agents.map(a=>[a.id,a.instance_name||a.name,a.kind,!!a.listener?.active,a.wake?.configured]));
    if(key!==state.directoryKey){
      select.replaceChildren(new Option('Selecciona una instancia',''));
      for(const agent of state.agents) { const wake=agent.listener?.active?(agent.wake?.configured?'wake configurado':'sin wake'):'sin señal';select.add(new Option(`${agent.instance_name || agent.name} · ${wake}`,agent.id)); }
      if(state.agents.some(a=>a.id===selected)) select.value=selected;
      state.directoryKey=key;
    }
    if(state.restore?.agent && state.agents.some(a=>a.id===state.restore.agent)){select.value=state.restore.agent;state.restore.agent=null;document.querySelector('.sidebar').classList.add('has-selection');byId('change-agent').setAttribute('aria-expanded','false');render(true);}
    renderAgentList();
    renderState();
  }
  function renderAgentList() {
    const list=byId('agent-list'), selected=byId('agent').value, query=byId('agent-search').value.trim().toLocaleLowerCase();
    const rows=state.agents.filter(a=>`${a.instance_name||a.name} ${a.kind} ${a.project||''}`.toLocaleLowerCase().includes(query));
    const key=JSON.stringify([selected,query,rows.map(a=>[a.id,a.instance_name||a.name,a.kind,a.project,!!a.listener?.active,a.wake?.configured])]);
    if(list.dataset.renderKey===key)return;
    list.dataset.renderKey=key;list.replaceChildren();
    if(!rows.length){const empty=document.createElement('p');empty.className='agent-empty';empty.textContent=state.agents.length?'No hay agentes con ese nombre.':'Aún no hay agentes registrados.';list.append(empty);return;}
    for(const agent of rows){
      const name=agent.instance_name||agent.name;const ready=!!agent.listener?.active&&!!agent.wake?.configured;
      const button=document.createElement('button');button.type='button';button.className='agent-item';button.setAttribute('aria-pressed',String(agent.id===selected));
      const icon=document.createElement('span');icon.className='agent-initial';icon.setAttribute('aria-hidden','true');icon.textContent=(name.trim()[0]||'A').toUpperCase();
      const copy=document.createElement('span');copy.className='agent-copy';const title=document.createElement('span');title.className='agent-name';title.textContent=name;
      const detail=document.createElement('span');detail.className='agent-detail';detail.textContent=ready?'Despertador configurado':agent.listener?.active?'Listener activo · sin wake':'Sin señal del listener';
      copy.append(title,detail);const dot=document.createElement('span');dot.className='agent-indicator'+(ready?' ready':'');dot.setAttribute('aria-hidden','true');
      button.append(icon,copy,dot);button.onclick=()=>{byId('agent').value=agent.id;document.querySelector('.sidebar').classList.add('has-selection');document.querySelector('.sidebar').classList.remove('show-chooser');byId('change-agent').setAttribute('aria-expanded','false');renderAgentList();renderState();render(true);};list.append(button);
    }
  }
  function renderState() {
    const agent=state.agents.find(a=>a.id===byId('agent').value), label=byId('agent-state');
    byId('send').disabled=!agent||state.sending;
    byId('title').textContent=agent ? (agent.instance_name || agent.name) : 'Elige un agente';
    if(!agent){label.textContent='Selecciona una instancia para ver su estado.';byId('subtitle').textContent='Selecciona una instancia para comenzar.';return;}
    const listener=agent.listener?.active, wake=agent.wake?.configured;
    label.textContent=`Listener: ${listener?'activo':'sin señal'} · Despertador: ${!listener?'desconocido':wake?'configurado, sin verificar':'no configurado'}`;
    byId('subtitle').textContent=`${agent.kind} · ${agent.project || 'Sin proyecto'} · ID ${agent.id}`;
  }
  async function addAttachment(box, message, file) {
    const link=document.createElement('a'); link.href='#';link.textContent=`Adjunto · ${file.filename}`;
    link.onclick=async event=>{event.preventDefault();try{const data=await api(`/dashboard/chat/attachments/${message.id}/${file.index}`);const binary=atob(data.data_base64);const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes],{type:data.media_type}));if(/^image\/(png|jpeg|gif|webp)$/.test(data.media_type)){state.blobs.push(url);const preview=document.createElement('img');preview.src=url;preview.alt=data.filename;box.append(preview);}else{const download=document.createElement('a');download.href=url;download.download=data.filename;download.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}}catch(error){byId('status').textContent=error.message;}};
    box.append(link);
  }
  function render(forceBottom=false) {
    const id=byId('agent').value, thread=byId('thread');const oldTop=thread.scrollTop;const atBottom=thread.scrollHeight-thread.clientHeight-oldTop<80;
    state.blobs.forEach(url=>URL.revokeObjectURL(url));state.blobs=[];thread.replaceChildren();
    const messages=state.messages.filter(m=>(m.from_agent_id===id || m.to_agent_id===id));
    if(!id || !messages.length){const empty=document.createElement('div');empty.className='empty';const title=document.createElement('strong');title.textContent=id?'Comienza una conversación':'Tu conversación empieza aquí';const hint=document.createElement('span');hint.textContent=id?'Envía una solicitud a esta instancia.':'Elige una instancia y envíale un mensaje.';empty.append(title,hint);thread.append(empty);return;}
    for(const message of messages){
      const mine=message.from_agent_id===state.humanId;
      const bubble=document.createElement('div');bubble.className='bubble'+(mine?' mine':'');
      const text=document.createElement('div');text.textContent=mine?message.text.replace(/^Mensaje del usuario autenticado: [^\n]*\n\n/,''):message.text;bubble.append(text);
      for(const file of message.attachments||[]) addAttachment(bubble,message,file);
      const meta=document.createElement('small');meta.textContent=`${mine?'Tú':'Agente'} · ${new Date(message.created_at*1000).toLocaleString('es-CO')} · ${message.delivery_mode==='wake'?'despertar solicitado':mine?'enviado al buzón':'respuesta'}`;bubble.append(meta);thread.append(bubble);
    }
    thread.scrollTop=forceBottom||atBottom?thread.scrollHeight:oldTop;
  }
  async function refresh() {
    if(state.busy)return;state.busy=true;
    try {
      let more, changed=false;
      do { const data=await api(`/dashboard/chat/data?after_id=${state.cursor}`);state.agents=data.agents;state.humanId=data.human_instance_id||state.humanId;state.messages.push(...data.messages);changed ||= data.messages.some(m=>m.from_agent_id===byId('agent').value||m.to_agent_id===byId('agent').value);state.cursor=data.next_after_id;more=data.has_more; } while(more);
      updateDirectory();if(changed)render();byId('sync').textContent='Actualizado · cada 5 s';
    } catch(error){byId('sync').textContent=error.status===403?'Sin acceso al chat':'Sin conexión · reintentando';if(error.status===403)byId('status').textContent='Tu cuenta no tiene acceso al chat. Vuelve al panel y usa una cuenta autorizada.';} finally {state.busy=false;}
  }
  const readFile=file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({filename:file.name,media_type:file.type||'application/octet-stream',data_base64:String(reader.result).split(',')[1]});reader.onerror=()=>reject(new Error('No se pudo leer el archivo'));reader.readAsDataURL(file);});
  byId('agent').onchange=()=>{renderState();render(true);};
  byId('agent-search').addEventListener('input',renderAgentList);
  byId('change-agent').onclick=()=>{const sidebar=document.querySelector('.sidebar');const expanded=sidebar.classList.toggle('show-chooser');byId('change-agent').setAttribute('aria-expanded',String(expanded));if(expanded)byId('agent-search').focus();};
  byId('mode').onchange=()=>{const waking=byId('mode').value==='wake';byId('backlog-label').hidden=!waking;byId('delivery-help').textContent=waking?'Se pedirá al adaptador que abra el chat. Depende del host y puede consumir un turno.':'El mensaje quedará guardado hasta que el agente revise su buzón.';};
  byId('composer').onsubmit=async event=>{
    event.preventDefault();const agent=byId('agent').value, text=byId('text').value, files=[...byId('files').files];
    if(!agent){byId('status').textContent='Selecciona una instancia.';return;}
    if(!text.trim()&&!files.length){byId('status').textContent='Escribe un mensaje o adjunta un archivo.';return;}
    if(files.length>3||files.reduce((sum,f)=>sum+f.size,0)>8*1024*1024){byId('status').textContent='Máximo 3 archivos y 8 MiB en total.';return;}
    const signature=JSON.stringify([agent,text,byId('mode').value,byId('backlog').checked,files.map(f=>[f.name,f.size,f.lastModified])]);
    if(!state.draft||state.draft.signature!==signature)state.draft={signature,key:crypto.randomUUID()};
    state.sending=true;byId('send').disabled=true;
    try {const attachments=await Promise.all(files.map(readFile));await api('/dashboard/chat/messages','POST',{to_agent_id:agent,text,delivery_mode:byId('mode').value,wake_backlog:byId('mode').value==='wake'&&byId('backlog').checked,attachments,idempotency_key:state.draft.key});state.draft=null;byId('text').value='';byId('files').value='';byId('status').textContent='';await refresh();}
    catch(error){byId('status').textContent=`No se pudo enviar: ${error.message}`;}finally{state.sending=false;renderState();}
  };
  async function checkRelease(){try{const result=await api('/health');if(state.release&&state.release!==result.version)byId('update-banner').hidden=false;else if(!state.release)state.release=result.version;}catch{}}
  byId('update-chat').onclick=()=>{if(state.sending){byId('status').textContent='Espera a que termine el envío antes de actualizar.';return;}try{sessionStorage.setItem('mailbox-chat-update-draft',JSON.stringify({text:byId('text').value,agent:byId('agent').value,mode:byId('mode').value,backlog:byId('backlog').checked,files:byId('files').files.length>0}));location.reload();}catch{byId('status').textContent='No se pudo guardar el borrador; copia el texto antes de actualizar.';}};
  try{const saved=sessionStorage.getItem('mailbox-chat-update-draft');if(saved){state.restore=JSON.parse(saved);sessionStorage.removeItem('mailbox-chat-update-draft');byId('text').value=state.restore.text||'';byId('mode').value=state.restore.mode||'inbox';byId('backlog').checked=!!state.restore.backlog;byId('mode').onchange();if(state.restore.files)byId('status').textContent='El texto se conservó. Vuelve a adjuntar los archivos.';}}catch{}
  refresh();checkRelease();setInterval(refresh,5000);setInterval(checkRelease,60000);
})();
