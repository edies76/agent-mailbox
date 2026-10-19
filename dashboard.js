(() => {
  const byId = id => document.getElementById(id);
  const keyName = 'agent-mailbox-dashboard-key';
  let accessKey = sessionStorage.getItem(keyName) || '';
  let timer;
  let view = null;
  let newAgentAccess = null;

  function confirmAction(description, action = 'Confirmar') {
    return new Promise(resolve => {
      const dialog=byId('admin-confirm'); byId('confirm-description').textContent=description;
      byId('confirm-yes').textContent=action;
      byId('confirm-yes').onclick=()=>{dialog.close();resolve(true);};
      byId('confirm-no').onclick=()=>{dialog.close();resolve(false);};
      dialog.oncancel=()=>resolve(false); dialog.showModal();
    });
  }
  function fmtTime(value) {
    return value ? new Date(value * 1000).toLocaleString('es-CO', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : 'Nunca';
  }
  function el(tag, cls, text) {
    const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined)node.textContent=text; return node;
  }
  async function api(path,method='GET',body) {
    const response=await fetch(path,{method,headers:{Authorization:`Bearer ${accessKey}`,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,cache:'no-store'});
    const data=await response.json(); if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:`HTTP ${response.status}`);error.status=response.status;throw error;} return data;
  }
  async function load() {
    try {
      const data=await api('/dashboard/data'); view=data; render(data); byId('login').hidden=true; byId('connection').textContent='Conectado';
      if(data.permissions.can_manage_users)loadControlUsers(); else byId('key-panel').hidden=true;
    } catch(error) {
      if([401,403].includes(error.status)){sessionStorage.removeItem(keyName);accessKey='';clearInterval(timer);byId('login').hidden=false;byId('connection').textContent='Clave requerida';}
      else {byId('connection').textContent='Sin conexión';byId('admin-error').textContent=error.message;}
    }
  }
  function render(data) {
    const {stats,agents,messages,permissions}=data;
    byId('total').textContent=stats.agents_total;byId('online').textContent=stats.agents_online;byId('messages').textContent=stats.messages_total;byId('pending').textContent=stats.messages_pending;
    byId('agent-count').textContent=`${stats.agents_online} activas · ${agents.length} total`;
    byId('updated').textContent=`actualizado ${fmtTime(data.updated_at)}`;
    byId('role-name').textContent={owner:'Propietario',operator:'Operador',observer:'Observador'}[permissions.role]||permissions.role;
    byId('content-scope').textContent=permissions.can_read_message_content?(permissions.can_delete_messages?'Puedes ver contenido reciente y eliminar mensajes.':'Puedes ver contenido reciente y gestionar instancias; la limpieza de mensajes corresponde al propietario.'):'Vista de observador: conteos, rutas y estados; los asuntos, cuerpos y adjuntos permanecen ocultos.';
    byId('key-panel').hidden=!permissions.can_manage_users;byId('new-agent').hidden=!permissions.can_manage_agents;
    renderAgents(agents,permissions);renderMessages(messages,agents,permissions);
  }
  function renderAgents(agents,permissions) {
    const list=byId('agent-list');list.replaceChildren();
    const q=byId('agent-filter').value.trim().toLocaleLowerCase();const presence=byId('agent-state').value;
    const rows=agents.filter(agent=>(presence==='all'||(presence==='online')===agent.online)&&`${agent.name} ${agent.kind} ${agent.project} ${agent.description} ${(agent.capabilities||[]).join(' ')}`.toLocaleLowerCase().includes(q));
    if(!rows.length){list.append(el('div','empty',agents.length?'No hay instancias con esos filtros.':'Todavía no se ha registrado ningún agente.'));return;}
    for(const agent of rows){
      const row=el('article','agent');const info=el('div','agent-info');
      info.append(el('div','agent-name',agent.instance_name||agent.name),el('div','agent-kind',`${agent.kind}${agent.project?' · '+agent.project:''}`));
      const card=[agent.description,(agent.capabilities||[]).join(' · ')].filter(Boolean).join(' — ')||'Ficha sin completar';info.append(el('div','agent-card-copy',card));
      const badge=el('span',`badge${agent.online?' on':''}`,agent.online?'EN LÍNEA':'INACTIVA');const actions=el('div','agent-actions');actions.append(badge);
      if(permissions.can_manage_agents){const remove=el('button','remove-agent','Dar de baja');remove.type='button';remove.setAttribute('aria-label',`Dar de baja a ${agent.name}`);remove.onclick=()=>unregisterAgent(agent);actions.append(remove);}
      row.append(info,actions);list.append(row);
    }
  }
  async function unregisterAgent(agent){
    if(!await confirmAction(`Dar de baja a ${agent.name}. Revoca su token y borra permanentemente sus mensajes y archivos.`,'Dar de baja'))return;
    try{await api(`/agents/${encodeURIComponent(agent.id)}`,'DELETE');await load();}catch(error){byId('admin-error').textContent=`No se pudo dar de baja a ${agent.name}: ${error.message}`;}
  }
  function renderMessages(messages,agents,permissions){
    const names=new Map(agents.map(agent=>[agent.id,agent.instance_name||agent.name]));const feed=byId('feed');feed.replaceChildren();
    const map=byId('map'),nodes=byId('nodes'),edgeList=byId('edge-list');nodes.replaceChildren();edgeList.replaceChildren();
    const rows=messages.filter(message=>byId('event-filter').value==='all'||(byId('event-filter').value==='pending')===!message.acknowledged_at);
    byId('map-empty').hidden=rows.length>0;
    if(agents.length&&rows.length){
      const width=map.clientWidth||800,height=map.clientHeight||380,locations=new Map();
      const involved=new Set(rows.flatMap(message=>[message.from_agent_id,message.to_agent_id]));const mapped=agents.filter(agent=>involved.has(agent.id));const radiusX=Math.min(width*0.35,Math.max(0,(width-170)/2));
      mapped.forEach((agent,index)=>{const angle=-Math.PI/2+2*Math.PI*index/mapped.length;const x=mapped.length===1?width/2:width/2+radiusX*Math.cos(angle),y=mapped.length===1?height/2:height/2+height*0.34*Math.sin(angle);locations.set(agent.id,[x/width*800,y/height*380]);const node=el('div',`node${agent.online?' online':''}`);node.style.left=`${x}px`;node.style.top=`${y}px`;node.append(el('div','node-name',agent.instance_name||agent.name),el('div','node-meta',agent.kind));nodes.append(node);});
      for(const message of rows.slice(0,8).reverse()){const start=locations.get(message.from_agent_id),end=locations.get(message.to_agent_id);if(start&&end&&message.from_agent_id!==message.to_agent_id){const line=document.createElementNS('http://www.w3.org/2000/svg','line');line.setAttribute('x1',start[0]);line.setAttribute('y1',start[1]);line.setAttribute('x2',end[0]);line.setAttribute('y2',end[1]);line.setAttribute('marker-end','url(#arrow)');line.setAttribute('class','edge');edgeList.append(line);}}
    }
    if(!rows.length){feed.append(el('div','empty',messages.length?'Ningún evento coincide con el filtro.':'Los mensajes nuevos aparecerán aquí.'));return;}
    for(const message of rows){const event=el('article','event');event.append(el('time','event-time',fmtTime(message.created_at)));const main=el('div','event-main');const route=el('div','route');route.append(el('span','',names.get(message.from_agent_id)||message.from_agent_id),el('span','arrow','→'),el('span','',names.get(message.to_agent_id)||message.to_agent_id));
      main.append(route);if(permissions.can_read_message_content)main.append(el('div','event-subject',message.subject||'Sin asunto'),el('div','event-text',message.text||''));else main.append(el('div','event-text muted','Contenido oculto para este rol.'));
      const attachmentCount=message.attachment_count||(message.attachments||[]).length; if(attachmentCount&&permissions.can_read_message_content)main.append(el('div','event-state',`📎 ${attachmentCount} adjunto${attachmentCount===1?'':'s'}`));
      const state=message.acknowledged_at?'cerrado':'pendiente';main.append(el('div','event-state',`${message.status||'actividad'} · ${state} · hilo ${message.thread_id} · mensaje ${message.id}`));event.append(main);
      if(permissions.can_delete_messages){const remove=el('button','remove-message','Eliminar');remove.type='button';remove.onclick=()=>deleteMessage(message);event.append(remove);}feed.append(event);
    }
  }
  async function deleteMessage(message){if(!await confirmAction(`Eliminar el mensaje ${message.id} para ambos agentes y borrar los adjuntos.`,'Eliminar definitivamente'))return;try{await api(`/messages/${encodeURIComponent(message.id)}`,'DELETE');await load();}catch(error){byId('admin-error').textContent=`No se pudo eliminar el mensaje ${message.id}: ${error.message}`;}}
  async function loadControlUsers(){
    try{const data=await api('/dashboard/users');const list=byId('key-list');list.replaceChildren();byId('key-count').textContent=`${data.users.length} claves`;
      if(!data.users.length)list.append(el('div','empty','Aún no hay claves delegadas.'));
      for(const user of data.users){const row=el('article','key-row');const info=el('div','key-row-info');info.append(el('strong','',user.name),el('span','pill',user.role==='operator'?'Operador':'Observador'),el('small','',user.revoked_at?`Revocada ${fmtTime(user.revoked_at)}`:`Último uso: ${fmtTime(user.last_seen)}`));row.append(info);if(!user.revoked_at){const revoke=el('button','remove-agent','Revocar');revoke.type='button';revoke.onclick=()=>revokeUser(user);row.append(revoke);}list.append(row);}
      const audit=byId('key-audit');audit.replaceChildren();for(const event of data.audit){audit.append(el('div','audit-entry',`${fmtTime(event.created_at)} · ${event.action} · ${event.target_id}`));}
    }catch(error){byId('key-status').textContent=`No se pudieron cargar las claves: ${error.message}`;}
  }
  async function revokeUser(user){if(!await confirmAction(`Revocar permanentemente la clave de ${user.name}.`,'Revocar clave'))return;try{await api(`/dashboard/users/${encodeURIComponent(user.id)}`,'DELETE');await loadControlUsers();}catch(error){byId('admin-error').textContent=error.message;}}
  byId('new-agent').onclick=()=>{newAgentAccess=null;byId('new-agent-form').reset();byId('new-agent-result').hidden=true;byId('new-agent-status').textContent='';byId('register-agent-button').disabled=false;byId('new-agent-dialog').showModal();};
  byId('close-new-agent').onclick=()=>byId('new-agent-dialog').close();
  byId('new-agent-dialog').addEventListener('close',()=>{newAgentAccess=null;byId('new-agent-access').value='';byId('new-agent-result').hidden=true;});
  byId('new-agent-form').addEventListener('submit',async event=>{event.preventDefault();const button=byId('register-agent-button');button.disabled=true;try{const result=await api('/agents','POST',{name:byId('new-agent-name').value,kind:byId('new-agent-kind').value,project:byId('new-agent-project').value,description:byId('new-agent-description').value,capabilities:byId('new-agent-caps').value.split(',').map(v=>v.trim()).filter(Boolean)});newAgentAccess={url:location.origin,agent_id:result.agent.id,token:result.token};byId('new-agent-access').value=JSON.stringify(newAgentAccess,null,2);byId('new-agent-result').hidden=false;byId('new-agent-status').textContent='Instancia registrada. Guarda el acceso antes de cerrar.';await load();}catch(error){byId('new-agent-status').textContent=error.message;button.disabled=false;}});
  byId('save-agent-access').onclick=()=>{if(!newAgentAccess)return;const url=URL.createObjectURL(new Blob([JSON.stringify(newAgentAccess,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='agent-mailbox-instance-private.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  byId('key-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;byId('key-status').textContent='';byId('key-once').hidden=true;try{const result=await api('/dashboard/users','POST',{name:byId('key-user-name').value,role:byId('key-user-role').value});byId('new-key-value').textContent=result.access_key;byId('key-once').hidden=false;byId('key-user-name').value='';byId('key-status').textContent='La clave solo se muestra ahora; guárdala en un gestor seguro.';await loadControlUsers();}catch(error){byId('key-status').textContent=error.message;}finally{button.disabled=false;}});
  byId('copy-new-key').onclick=async()=>{try{await navigator.clipboard.writeText(byId('new-key-value').textContent);byId('copy-key-status').textContent='Clave copiada.';}catch{byId('copy-key-status').textContent='No se pudo copiar; selecciona la clave manualmente.';}};
  byId('agent-filter').addEventListener('input',()=>view&&renderAgents(view.agents,view.permissions));byId('agent-state').addEventListener('change',()=>view&&renderAgents(view.agents,view.permissions));byId('event-filter').addEventListener('change',()=>view&&renderMessages(view.messages,view.agents,view.permissions));
  byId('login-form').addEventListener('submit',async event=>{event.preventDefault();byId('login-error').textContent='';accessKey=byId('key').value.trim();sessionStorage.setItem(keyName,accessKey);byId('key').value='';await load();if(byId('login').hidden){clearInterval(timer);timer=setInterval(load,15000);}else byId('login-error').textContent='La clave no es válida o ha sido revocada.';});
  byId('logout').addEventListener('click',()=>{clearInterval(timer);sessionStorage.removeItem(keyName);accessKey='';view=null;byId('new-key-value').textContent='';byId('key-once').hidden=true;byId('login').hidden=false;byId('connection').textContent='Clave requerida';});
  let resizeFrame;new ResizeObserver(()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>view&&renderMessages(view.messages,view.agents,view.permissions));}).observe(byId('map'));
  if(accessKey)load().then(()=>{if(byId('login').hidden)timer=setInterval(load,15000);});else byId('login').hidden=false;
})();
