(() => {
  const missingNode = {hidden:true,addEventListener(){},querySelector(){return null;},className:'',textContent:'',value:'',disabled:true,focus(){},select(){}};
  const byId = id => document.getElementById(id) || missingNode;
  let currentUser = null;
  let setupRequired = false;
  let timer;
  let view = null;
  let newAgentAccess = null;
  let newAgentNameTouched = false;
  let rotationPendingAgent = null;
  const agentRegistrationSecrets = new Map();
  function agentRecoverySecret(sessionId){const key=`${currentUser?.workspace_id||''}|${sessionId}`;if(!agentRegistrationSecrets.has(key)){const bytes=crypto.getRandomValues(new Uint8Array(32));agentRegistrationSecrets.set(key,btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=',''));}return agentRegistrationSecrets.get(key);}
  async function expectedRecoveryToken(secret,agentId){const encoder=new TextEncoder();const key=await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);const bytes=new Uint8Array(await crypto.subtle.sign('HMAC',key,encoder.encode(`agent-mailbox-instance:${agentId}`)));return [...bytes].map(byte=>byte.toString(16).padStart(2,'0')).join('');}
  {const button=byId('register-agent-button'),details=el('details'),summary=el('summary','','Reanudar una instancia existente'),label=el('label','','Token privado guardado de la instancia'),input=el('input');input.id='new-agent-proof';input.type='password';input.autocomplete='off';input.maxLength=256;label.htmlFor=input.id;details.append(summary,label,input);button.parentNode.insertBefore(details,button);}
  function suggestNewAgentName(){if(!newAgentNameTouched){const kind=byId('new-agent-kind').value.trim()||'Agent';const id=byId('new-agent-session-id').value;byId('new-agent-name').value=`${kind} · ${id.slice(0,8)}`;}}

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
  function csrfCookie(){
    const item=document.cookie.split('; ').find(value=>value.startsWith('__Host-agent-mailbox-csrf=')||value.startsWith('agent-mailbox-csrf='));
    return item?decodeURIComponent(item.slice(item.indexOf('=')+1)):'';
  }
  async function api(path,method='GET',body,extraHeaders={}) {
    const headers={...(body?{'Content-Type':'application/json'}:{}),...extraHeaders};
    if(!['GET','HEAD','OPTIONS'].includes(method))headers['X-CSRF-Token']=csrfCookie();
    const response=await fetch(path,{method,headers,body:body?JSON.stringify(body):undefined,cache:'no-store',credentials:'same-origin'});
    const data=await response.json(); if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:`HTTP ${response.status}`);error.status=response.status;throw error;} return data;
  }
  async function showLogin(){
    clearInterval(timer);currentUser=null;document.body.classList.add('auth-locked');
    try{const status=await api('/auth/status');setupRequired=status.setup_required;byId('setup-form').hidden=!setupRequired;byId('login-form').hidden=setupRequired;const localSetup=setupRequired&&status.local_setup_available;byId('setup-key-field').hidden=localSetup;byId('setup-key').required=!localSetup;byId('setup-local-hint').hidden=!localSetup;}
    catch{setupRequired=false;byId('setup-form').hidden=true;byId('login-form').hidden=false;}
    byId('access-key-form').hidden=true;byId('signup-form').hidden=true;
    byId('login').hidden=false;byId('connection').textContent='Inicia sesión';
  }
  function showAccessKeyLogin(){byId('login-form').hidden=true;byId('signup-form').hidden=true;byId('setup-form').hidden=true;byId('access-key-form').hidden=false;byId('access-key').focus();}
  function showPasswordLogin(){byId('access-key-form').hidden=true;byId('signup-form').hidden=true;byId('setup-form').hidden=!setupRequired;byId('login-form').hidden=setupRequired;}
  function showSignup(){byId('login-form').hidden=true;byId('setup-form').hidden=true;byId('access-key-form').hidden=true;byId('signup-form').hidden=false;byId('signup-error').textContent='';byId('signup-username').focus();}
  async function load() {
    try {
      const session=await api('/auth/session');currentUser={...session.user,auth_method:session.auth_method};
      const data=await api('/dashboard/data'); view=data; render(data); byId('login').hidden=true;document.body.classList.remove('auth-locked');byId('connection').textContent=`Conectado · ${currentUser.username}`;byId('change-password').hidden=currentUser.auth_method!=='password';
    } catch(error) {
      if(error.status===401){await showLogin();}
      else {byId('connection').textContent='Sin conexión';byId('admin-error').textContent=error.message;}
    }
  }
  function render(data) {
    const {stats,agents,messages,permissions}=data;
    byId('total').textContent=stats.agents_total;byId('online').textContent=stats.agents_online;byId('messages').textContent=stats.messages_total;byId('pending').textContent=stats.messages_pending;
    byId('agent-count').textContent=`${agents.length} en tu directorio compartido`;
    byId('updated').textContent=`actualizado ${fmtTime(data.updated_at)}`;
    byId('role-name').textContent={owner:'Propietario',workspace_owner:'Dueño de espacio',workspace_member:'Miembro de espacio',operator:'Operador',observer:'Observador'}[permissions.role]||permissions.role;
    byId('content-scope').textContent=permissions.role==='workspace_member'?'Miembro: ves y administras solo los agentes que registró tu cuenta; los buzones de otros miembros siguen privados.':permissions.can_read_message_content?(permissions.can_delete_messages?'Puedes ver contenido reciente y eliminar mensajes.':'Puedes ver contenido reciente y gestionar instancias; la limpieza de mensajes corresponde al propietario.'):'Vista de observador: conteos, rutas y estados; los asuntos, cuerpos y adjuntos permanecen ocultos.';
    byId('new-agent').hidden=!permissions.can_manage_agents;
    renderAgents(agents,permissions);renderMessages(messages,agents,permissions);
  }
  async function loadAccountProfile(){
    const panel=byId('account-profile');
    try{
      const data=await api('/account/profile');const account=data.account, workspace=data.workspace||{};
      panel.hidden=false;byId('profile-role').textContent={owner:'Propietario',workspace_owner:'Dueño de espacio',workspace_member:'Miembro de espacio',operator:'Operador',observer:'Observador'}[account.role]||account.role;
      byId('profile-username').textContent=account.username;byId('profile-workspace').textContent=workspace.name||account.workspace_id;byId('profile-account-id').textContent=account.id;
      const meta=byId('registration-key-meta'),create=byId('create-registration-key'),revoke=byId('revoke-registration-key');
      create.disabled=!data.registration.enabled;revoke.disabled=!data.registration.enabled||!data.registration_key;
      if(data.registration_key){meta.textContent=`Activa · ${data.registration_key.key_prefix}… · creada ${fmtTime(data.registration_key.created_at)}${data.registration_key.rotated_at?` · renovada ${fmtTime(data.registration_key.rotated_at)}`:''}`;}
      else meta.textContent=data.registration.enabled?'Todavía no has creado una clave.':'Tu rol puede consultar el perfil, pero no registrar agentes.';
    }catch(error){panel.hidden=true;}
  }
  async function loadProfileDirectory(){
    const panel=byId('profiles'),list=byId('profile-directory');
    try{const data=await api('/dashboard/profiles');panel.hidden=false;list.replaceChildren();if(!data.profiles.length){list.append(el('div','empty','Todavía no hay perfiles lógicos. Los agentes independientes seguirán apareciendo en el directorio de instancias.'));return;}for(const profile of data.profiles){const card=el('a','profile-directory-card');card.href=profile.route;const head=el('div','profile-directory-head');head.append(el('span','eyebrow',profile.kind),el('span','pill',`${profile.instances_count} instancia${profile.instances_count===1?'':'s'}`));card.append(head,el('h3','',profile.name),el('p','',profile.description||profile.project||'Sin descripción pública.'),el('small','',`${profile.online_count} en línea · abrir perfil ↗`));list.append(card);}}
    catch(error){panel.hidden=true;}
  }
  function renderAgents(agents,permissions) {
    const list=byId('agent-list');list.replaceChildren();
    const q=byId('agent-filter').value.trim().toLocaleLowerCase();const presence=byId('agent-state').value;
    const rows=agents.filter(agent=>(presence==='all'||(presence==='online')===agent.online)&&`${agent.name} ${agent.kind} ${agent.project} ${agent.chat_path||''} ${agent.description} ${(agent.capabilities||[]).join(' ')}`.toLocaleLowerCase().includes(q));
    if(!rows.length){list.append(el('div','empty',agents.length?'No hay instancias con esos filtros.':'Todavía no se ha registrado ningún agente.'));return;}
    for(const agent of rows){
      const row=el('article','agent');const info=el('div','agent-info');
      info.append(el('div','agent-name',agent.instance_name||agent.name),el('div','agent-kind',`${agent.kind}${agent.project?' · '+agent.project:''}`));
      if(agent.chat_path)info.append(el('div','agent-chat-path',agent.chat_path));
      const card=[agent.description,(agent.capabilities||[]).join(' · ')].filter(Boolean).join(' — ')||'Ficha sin completar';info.append(el('div','agent-card-copy',card));
      const provenance=agent.added_by||{};info.append(el('div','agent-provenance',`Añadido por ${provenance.username||'sistema'} · ${provenance.method||'registro directo'}`));
      if(agent.profile_id){const profileLink=el('a','agent-profile-link','Abrir perfil y sus instancias ↗');profileLink.href=`/dashboard/profile/${encodeURIComponent(agent.profile_id)}`;info.append(profileLink);}
      const badge=el('span',`badge${agent.online?' on':''}`,agent.online?'EN LÍNEA':'INACTIVA');const actions=el('div','agent-actions');actions.append(badge);
      const listener=agent.listener?.active;const wake=agent.wake?.configured;
      actions.append(el('span',`badge${listener?' on':''}`,listener?'LISTENER ACTIVO':'LISTENER SIN SEÑAL'));
      const wakeBadge=el('span',`badge${wake?' on':''}`,listener?(wake?'WAKE CONFIGURADO':'SIN WAKE'):'WAKE DESCONOCIDO');wakeBadge.title='Declarado por el listener; no garantiza que el host inicie un turno.';actions.append(wakeBadge);
      if(!agent.profile_id&&agent.kind!=='human'&&permissions.can_manage_agents&&agent.can_manage!==false){const convert=el('button','button small secondary','Crear perfil');convert.type='button';convert.onclick=()=>openConvertProfile(agent);actions.append(convert);}
      if(permissions.can_manage_agents&&agent.can_manage!==false){const rename=el('button','button small secondary','Renombrar');rename.type='button';rename.onclick=()=>openRenameAgent(agent);actions.append(rename);if(permissions.can_manage_workspace){const rotate=el('button','button small secondary','Renovar token');rotate.type='button';rotate.onclick=()=>rotateAgentToken(agent);actions.append(rotate);}const remove=el('button','remove-agent','Dar de baja');remove.type='button';remove.setAttribute('aria-label',`Dar de baja a ${agent.name}`);remove.onclick=()=>unregisterAgent(agent);actions.append(remove);}
      row.append(info,actions);list.append(row);
    }
  }
  async function openRenameAgent(agent){byId('rename-agent-id').value=agent.id;byId('rename-agent-name').value=agent.instance_name||agent.name;byId('rename-agent-status').textContent='';byId('rename-agent-dialog').showModal();}
  function openConvertProfile(agent){byId('convert-profile-agent').value=agent.id;byId('convert-profile-name').value=agent.name;byId('convert-profile-error').textContent='';byId('convert-profile-secret').value='';byId('convert-profile-entry').hidden=false;byId('convert-profile-result').hidden=true;byId('convert-profile-dialog').showModal();}
  byId('convert-profile-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;try{const result=await api(`/dashboard/agents/${encodeURIComponent(byId('convert-profile-agent').value)}/create-profile`,'POST',{name:byId('convert-profile-name').value.trim()});byId('convert-profile-secret').value=JSON.stringify({url:location.origin,profile_id:result.profile.id,enrollment_token:result.enrollment_token,kind:result.profile.kind,instance_prefix:result.profile.name},null,2);byId('convert-profile-entry').hidden=true;byId('convert-profile-result').hidden=false;await load();}catch(error){byId('convert-profile-error').textContent=error.message;}finally{button.disabled=false;}});
  byId('convert-profile-close').onclick=()=>byId('convert-profile-dialog').close();
  byId('convert-profile-done').onclick=()=>{byId('convert-profile-secret').value='';byId('convert-profile-dialog').close();};
  byId('convert-profile-copy').onclick=async()=>{try{await navigator.clipboard.writeText(byId('convert-profile-secret').value);byId('convert-profile-copy-status').textContent='Configuración copiada.';}catch{byId('convert-profile-secret').focus();byId('convert-profile-secret').select();byId('convert-profile-copy-status').textContent='Seleccionada; cópiala manualmente.';}};
  byId('convert-profile-dialog').addEventListener('cancel',event=>{if(!byId('convert-profile-result').hidden){event.preventDefault();byId('convert-profile-copy-status').textContent='Guarda la configuración y pulsa «Ya la guardé».';}});
  async function rotateAgentToken(agent){
    if(!await confirmAction(`Renovar el token de ${agent.name}. El agente y sus claves API se desconectarán hasta actualizar su configuración; su historial permanece.`,'Renovar token'))return;
    try{
      const pendingKey=`agent-mailbox-rotation:${agent.id}`;let pending=JSON.parse(sessionStorage.getItem(pendingKey)||'null');
      if(!pending){if(!Number.isInteger(agent.credential_epoch))throw new Error('Actualiza el panel antes de renovar el token.');const bytes=crypto.getRandomValues(new Uint8Array(32));pending={recovery_secret:btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=',''),expected_epoch:agent.credential_epoch};sessionStorage.setItem(pendingKey,JSON.stringify(pending));}
      const recoverySecret=pending.recovery_secret;
      const result=await api(`/dashboard/agents/${encodeURIComponent(agent.id)}/rotate-token`,'POST',{}, {'X-Mailbox-Recovery-Secret':recoverySecret,'X-Mailbox-Expected-Epoch':String(pending.expected_epoch)});
      if(result.token!==await expectedRecoveryToken(recoverySecret,agent.id))throw new Error('El servidor no confirmó la credencial privada.');
      rotationPendingAgent=agent.id;byId('rotated-agent-token').value=JSON.stringify({token:result.token,recovery_secret:recoverySecret},null,2);byId('rotated-agent-status').textContent='';byId('rotate-agent-dialog').showModal();await load();
    }catch(error){if(error.status===409){sessionStorage.removeItem(`agent-mailbox-rotation:${agent.id}`);await load();}byId('admin-error').textContent=error.message;}
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
      const wake=message.delivery_mode==='wake';const reminder=wake&&message.acknowledged_at?null:wake?'solicitado':null;
      const badges=el('div','event-badges');
      badges.append(el('span',`tag ${wake?'tag-wake':'tag-inbox'}`,wake?'Despertar':'Normal'));
      if(wake)badges.append(el('span',`tag ${message.acknowledged_at?'tag-ok':'tag-warn'}`,message.acknowledged_at?'Despertar atendido':'Despertar solicitado'));
      if(message.wake_backlog)badges.append(el('span','tag','Lee pendientes anteriores'));
      if(message.acknowledged_at)badges.append(el('span','tag tag-ok','Cerrado'));else badges.append(el('span','tag tag-warn','Pendiente'));
      const open=el('a','event-open');open.href=`/dashboard/message/${message.id}`;open.setAttribute('aria-label',`Ver la ruta completa del mensaje ${message.id}`);
      open.append(route);main.append(open,badges);
      if(permissions.can_read_message_content)main.append(el('div','event-subject',message.subject||'Sin asunto'),el('div','event-text',message.text||''));else main.append(el('div','event-text muted','Contenido oculto para este rol.'));
      const attachmentCount=message.attachment_count||(message.attachments||[]).length; if(attachmentCount&&permissions.can_read_message_content)main.append(el('div','event-state',`${attachmentCount} adjunto${attachmentCount===1?'':'s'}`));
      main.append(el('div','event-state',`${message.status||'actividad'} · hilo ${message.thread_id} · mensaje ${message.id} · ver ruta completa`));event.append(main);
      if(permissions.can_delete_messages){const remove=el('button','remove-message','Eliminar');remove.type='button';remove.onclick=()=>deleteMessage(message);event.append(remove);}feed.append(event);
    }
  }
  async function deleteMessage(message){if(!await confirmAction(`Eliminar el mensaje ${message.id} para ambos agentes y borrar los adjuntos.`,'Eliminar definitivamente'))return;try{await api(`/messages/${encodeURIComponent(message.id)}`,'DELETE');await load();}catch(error){byId('admin-error').textContent=`No se pudo eliminar el mensaje ${message.id}: ${error.message}`;}}
  async function loadAccounts(){
    try{const [data,auditData]=await Promise.all([api('/dashboard/accounts'),api('/dashboard/audit')]);const list=byId('key-list');list.replaceChildren();byId('key-count').textContent=`${data.accounts.filter(user=>!user.disabled_at).length} activas · ${data.accounts.length} total`;
      if(!data.accounts.length)list.append(el('div','empty','Aún no hay cuentas de equipo.'));
      for(const user of data.accounts){const row=el('article','key-row');const info=el('div','key-row-info');info.append(el('strong','',user.username),el('span','pill',user.role==='owner'?'Propietario':user.role==='operator'?'Operador':'Observador'),el('small','',user.disabled_at?`Desactivada ${fmtTime(user.disabled_at)}`:`Último acceso: ${fmtTime(user.last_login_at)}`));row.append(info);if(user.role!=='owner'&&!user.disabled_at){const disable=el('button','remove-agent','Desactivar');disable.type='button';disable.onclick=()=>disableAccount(user);row.append(disable);}list.append(row);}
      const audit=byId('key-audit');audit.replaceChildren();for(const event of auditData.events){audit.append(el('div','audit-entry',`${fmtTime(event.created_at)} · ${event.action} · ${event.target_id}`));}
    }catch(error){byId('key-status').textContent=`No se pudieron cargar las cuentas: ${error.message}`;}
  }
  async function disableAccount(user){if(!await confirmAction(`Desactivar la cuenta de ${user.username}. Sus sesiones dejarán de funcionar.`,'Desactivar cuenta'))return;try{await api(`/dashboard/accounts/${encodeURIComponent(user.id)}`,'DELETE');await loadAccounts();}catch(error){byId('admin-error').textContent=error.message;}}
  async function loadWorkspace(){
    try{
      const data=await api('/dashboard/workspace'),list=byId('workspace-connection-list');list.replaceChildren();
      const active=data.connections.filter(row=>row.status==='accepted');
      byId('workspace-connection-count').textContent=`${active.length} conectados`;
      if(!data.connections.length&&!data.invitations.length)list.append(el('div','empty','Aún no hay otros espacios conectados.'));
      for(const row of data.connections){const item=el('article','key-row'),info=el('div','key-row-info');info.append(el('strong','',row.workspace_name||row.workspace_id),el('span','pill',row.status==='accepted'?'Conectado':'Desconectado'),el('small','',row.workspace_id));item.append(info);if(row.status==='accepted'){const button=el('button','remove-agent','Desconectar');button.type='button';button.onclick=()=>disconnectWorkspace(row.workspace_id);item.append(button);}list.append(item);}
      for(const row of data.invitations){const state=row.used_at?'Usada':row.revoked_at?'Revocada':row.expires_at<=Date.now()/1000?'Vencida':'Pendiente';const item=el('article','key-row'),info=el('div','key-row-info');info.append(el('strong','',row.mode==='member'?'Miembro del workspace':'Workspace independiente'),el('span','pill',state),el('small','',`Vence ${fmtTime(row.expires_at)}`));item.append(info);if(state==='Pendiente'){const button=el('button','remove-agent','Revocar');button.type='button';button.onclick=()=>revokeWorkspaceInvite(row.id);item.append(button);}list.append(item);}
    }catch(error){byId('workspace-invite-status').textContent=`No se pudo cargar el espacio: ${error.message}`;}
  }
  async function disconnectWorkspace(id){if(!await confirmAction(`Desconectar ${id}. Sus agentes dejarán de aparecer y no podrán iniciar mensajes nuevos.`,'Desconectar espacio'))return;try{await api(`/dashboard/workspace-connections/${encodeURIComponent(id)}`,'DELETE');await load();await loadWorkspace();}catch(error){byId('workspace-invite-status').textContent=error.message;}}
  async function revokeWorkspaceInvite(id){if(!await confirmAction('Revocar esta invitación de un solo uso.','Revocar invitación'))return;try{await api(`/dashboard/workspace-invitations/${encodeURIComponent(id)}`,'DELETE');await loadWorkspace();}catch(error){byId('workspace-invite-status').textContent=error.message;}}
  async function loadLegacyControlUsers(){
    try{const data=await api('/dashboard/users'),list=byId('legacy-key-list');list.replaceChildren();byId('legacy-key-count').textContent=`${data.users.length} claves`;
      if(!data.users.length)list.append(el('div','empty','No quedan claves Bearer heredadas.'));
      for(const user of data.users){const row=el('article','key-row'),info=el('div','key-row-info');info.append(el('strong','',user.name),el('span','pill',user.role==='operator'?'Operador':'Observador'),el('small','',user.revoked_at?`Revocada ${fmtTime(user.revoked_at)}`:`Último uso: ${fmtTime(user.last_seen)}`));row.append(info);if(!user.revoked_at){const revoke=el('button','remove-agent','Revocar');revoke.type='button';revoke.onclick=()=>revokeLegacyControlKey(user);row.append(revoke);}list.append(row);}
    }catch(error){byId('legacy-key-status').textContent=`No se pudieron cargar claves antiguas: ${error.message}`;}
  }
  async function revokeLegacyControlKey(user){if(!await confirmAction(`Revocar la clave Bearer de ${user.name}.`,'Revocar clave'))return;try{await api(`/dashboard/users/${encodeURIComponent(user.id)}`,'DELETE');await loadLegacyControlUsers();}catch(error){byId('admin-error').textContent=error.message;}}
  byId('new-agent-new-session').onclick=()=>{byId('new-agent-session-id').value=crypto.randomUUID();suggestNewAgentName();};
  byId('new-agent-session-id').addEventListener('input',suggestNewAgentName);
  byId('new-agent-name').addEventListener('input',()=>{newAgentNameTouched=true;});
  byId('new-agent-kind').addEventListener('input',suggestNewAgentName);
  byId('new-agent').onclick=()=>{newAgentAccess=null;byId('new-agent-form').reset();newAgentNameTouched=false;byId('new-agent-session-id').value=crypto.randomUUID();suggestNewAgentName();byId('new-agent-result').hidden=true;byId('new-agent-status').textContent='';byId('register-agent-button').disabled=false;byId('new-agent-dialog').showModal();};
  byId('close-new-agent').onclick=()=>byId('new-agent-dialog').close();
  byId('new-agent-dialog').addEventListener('close',()=>{newAgentAccess=null;byId('new-agent-access').value='';byId('new-agent-onboarding').value='';byId('new-agent-proof').value='';byId('new-agent-result').hidden=true;});
  byId('new-agent-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=byId('register-agent-button');button.disabled=true;
    const sessionId=byId('new-agent-session-id').value, savedToken=byId('new-agent-proof').value.trim();
    const recoverySecret=savedToken?'':agentRecoverySecret(sessionId);
    const proofHeaders=savedToken?{'X-Mailbox-Instance-Token':savedToken}:{'X-Mailbox-Recovery-Secret':recoverySecret};
    try{
      const capabilities=await api('/capabilities');if(capabilities.secure_registration?.version!==1)throw new Error('Actualiza el servidor antes de registrar esta instancia de forma segura.');
      const result=await api('/agents/secure','POST',{name:byId('new-agent-name').value,kind:byId('new-agent-kind').value,project:byId('new-agent-project').value,chat_path:byId('new-agent-chat-path').value,description:byId('new-agent-description').value,capabilities:byId('new-agent-caps').value.split(',').map(v=>v.trim()).filter(Boolean),session_key:sessionId},proofHeaders);
      const expected=savedToken||await expectedRecoveryToken(recoverySecret,result.agent.id);if(result.token!==expected)throw new Error('El servidor no confirmó la credencial privada.');
      newAgentAccess={url:location.origin,mcp_url:`${location.origin}/mcp`,transport:'streamable-http',agent_id:result.agent.id,session_id:result.session_id,token:result.token,onboarding_url:'/agent-mailbox.md',onboarding_manifest_url:'/onboarding'};
      if(recoverySecret)newAgentAccess.recovery_secret=recoverySecret;
      byId('new-agent-access').value=JSON.stringify(newAgentAccess,null,2);byId('new-agent-onboarding').value=`Servidor: ${location.origin}\nID de sesión: ${result.session_id}\nMCP (si permite credenciales por chat): ${location.origin}/mcp\n\n${result.onboarding.prompt}`;byId('new-agent-result').hidden=false;byId('new-agent-status').textContent=result.resumed?'Instancia recuperada con su acceso privado.':'Instancia creada con un ID de sesión privado.';await load();
    }catch(error){byId('new-agent-status').textContent=error.message;button.disabled=false;}
  });
  byId('save-agent-access').onclick=()=>{if(!newAgentAccess)return;const url=URL.createObjectURL(new Blob([JSON.stringify(newAgentAccess,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='agent-mailbox-instance-private.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  byId('copy-agent-onboarding').onclick=async()=>{try{await navigator.clipboard.writeText(byId('new-agent-onboarding').value);byId('copy-agent-onboarding').textContent='Instrucciones copiadas';}catch{byId('new-agent-onboarding').focus();byId('new-agent-onboarding').select();byId('copy-agent-onboarding').textContent='Seleccionadas; copia el texto';}};
  byId('rename-agent-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;const status=byId('rename-agent-status');try{await api(`/dashboard/agents/${encodeURIComponent(byId('rename-agent-id').value)}`,'PATCH',{name:byId('rename-agent-name').value});status.className='status success';status.textContent='Nombre actualizado. La identidad y su buzón siguen igual.';await load();}catch(error){status.className='status error';status.textContent=error.message;}finally{button.disabled=false;}});
  byId('account-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;byId('key-status').textContent='Creando cuenta…';try{const result=await api('/dashboard/accounts','POST',{username:byId('account-user-name').value,password:byId('account-user-password').value,role:byId('account-user-role').value});byId('account-user-password').value='';byId('key-status').className='status success';byId('key-status').textContent=`Cuenta ${result.username} creada. Entrará con la contraseña que acabas de asignar.`;await loadAccounts();}catch(error){byId('key-status').className='status error';byId('key-status').textContent=error.message;}finally{button.disabled=false;}});
  byId('workspace-invite-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;byId('workspace-invite-status').textContent='Creando invitación…';try{const result=await api('/dashboard/workspace-invitations','POST',{expires_in_hours:Number(byId('workspace-invite-expiry').value)});byId('workspace-invite-link').textContent=new URL(result.url,location.origin).toString();byId('workspace-invite-once').hidden=false;byId('workspace-invite-status').className='status success';byId('workspace-invite-status').textContent='Enlace de un solo uso creado. Compártelo por un canal privado.';await loadWorkspace();}catch(error){byId('workspace-invite-status').className='status error';byId('workspace-invite-status').textContent=error.message;}finally{button.disabled=false;}});
  byId('member-invite-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;byId('member-invite-status').textContent='Creando invitación…';try{const result=await api('/dashboard/member-invitations','POST',{expires_in_hours:Number(byId('member-invite-expiry').value),mode:'member'});byId('member-invite-link').textContent=new URL(result.url,location.origin).toString();byId('member-invite-once').hidden=false;byId('member-invite-status').className='status success';byId('member-invite-status').textContent='Invitación de miembro creada.';await loadWorkspace();}catch(error){byId('member-invite-status').className='status error';byId('member-invite-status').textContent=error.message;}finally{button.disabled=false;}});
  byId('workspace-connect-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;const status=byId('workspace-connect-status');status.textContent='Validando invitación…';try{const result=await api('/dashboard/workspace-connections','POST',{invite_code:byId('workspace-incoming-code').value.trim()});byId('workspace-incoming-code').value='';status.className='status success';status.textContent=`Conectado con ${result.workspace_name}.`;await load();await loadWorkspace();}catch(error){status.className='status error';status.textContent=error.message;}finally{button.disabled=false;}});
  byId('copy-workspace-invite').onclick=async()=>{try{await navigator.clipboard.writeText(byId('workspace-invite-link').textContent);byId('copy-workspace-invite').textContent='Copiado';}catch{byId('workspace-invite-status').textContent='Selecciona y copia el enlace manualmente.';}};
  byId('copy-member-invite').onclick=async()=>{try{await navigator.clipboard.writeText(byId('member-invite-link').textContent);byId('copy-member-invite').textContent='Copiado';}catch{byId('member-invite-status').textContent='Selecciona y copia el enlace manualmente.';}};
  byId('copy-rotated-agent-token').onclick=async()=>{try{await navigator.clipboard.writeText(byId('rotated-agent-token').value);byId('rotated-agent-status').textContent='Token copiado.';}catch{byId('rotated-agent-token').focus();byId('rotated-agent-token').select();byId('rotated-agent-status').textContent='Seleccionado; copia el token manualmente.';}};
  byId('confirm-rotated-agent').onclick=()=>{if(rotationPendingAgent)sessionStorage.removeItem(`agent-mailbox-rotation:${rotationPendingAgent}`);rotationPendingAgent=null;byId('rotate-agent-dialog').close();};
  byId('rotate-agent-dialog').addEventListener('close',()=>{byId('rotated-agent-token').value='';});
  byId('legacy-key-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;try{const result=await api('/dashboard/users','POST',{name:byId('legacy-key-name').value,role:byId('legacy-key-role').value});byId('legacy-key-value').textContent=result.access_key;byId('legacy-key-once').hidden=false;byId('legacy-key-name').value='';byId('legacy-key-status').textContent='Clave creada y visible una sola vez.';await loadLegacyControlUsers();}catch(error){byId('legacy-key-status').textContent=error.message;}finally{button.disabled=false;}});
  byId('registration-key-form').addEventListener('submit',async event=>{event.preventDefault();const button=byId('create-registration-key'),status=byId('registration-key-status');button.disabled=true;status.className='status';status.textContent='Generando clave…';try{const result=await api('/account/registration-key','POST',{});byId('registration-key-value').textContent=result.registration_key;byId('registration-key-once').hidden=false;status.className='status success';status.textContent='Clave creada. Cópiala y guárdala en el gestor de secretos del agente.';await loadAccountProfile();}catch(error){status.className='status error';status.textContent=error.message;}finally{button.disabled=false;}});
  byId('revoke-registration-key').addEventListener('click',async()=>{if(!await confirmAction('Revocar la clave de inscripción. Los agentes que aún no se hayan registrado ya no podrán usarla.','Revocar clave'))return;const status=byId('registration-key-status');try{await api('/account/registration-key','DELETE');byId('registration-key-once').hidden=true;status.className='status success';status.textContent='Clave revocada.';await loadAccountProfile();}catch(error){status.className='status error';status.textContent=error.message;}});
  byId('copy-registration-key').addEventListener('click',async()=>{const value=byId('registration-key-value').textContent;try{await navigator.clipboard.writeText(value);byId('registration-key-status').textContent='Clave copiada.';}catch{byId('registration-key-value').focus();}});
  byId('change-password').onclick=()=>{byId('password-status').textContent='';byId('password-form').reset();byId('password-dialog').showModal();};
  byId('password-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;const status=byId('password-status');try{await api('/auth/password','POST',{current_password:byId('current-password').value,new_password:byId('new-password').value});event.currentTarget.reset();status.className='status success';status.textContent='Contraseña actualizada. Se cerraron las demás sesiones.';await load();}catch(error){status.className='status error';status.textContent=error.message;}finally{button.disabled=false;}});
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>byId(button.dataset.close).close()));
  byId('agent-filter').addEventListener('input',()=>view&&renderAgents(view.agents,view.permissions));byId('agent-state').addEventListener('change',()=>view&&renderAgents(view.agents,view.permissions));byId('event-filter').addEventListener('change',()=>view&&renderMessages(view.messages,view.agents,view.permissions));
  byId('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;byId('login-error').textContent='';try{await api('/auth/login','POST',{username:byId('account-username').value,password:byId('account-password').value});byId('account-password').value='';await load();if(byId('login').hidden){clearInterval(timer);timer=setInterval(load,15000);}}catch(error){byId('login-error').textContent=error.message;}finally{button.disabled=false;}});
  byId('use-access-key').onclick=showAccessKeyLogin;byId('setup-use-access-key').onclick=showAccessKeyLogin;byId('use-password-login').onclick=showPasswordLogin;
  byId('use-signup').onclick=showSignup;byId('signup-back').onclick=showPasswordLogin;
  byId('access-key-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;byId('access-key-error').textContent='';try{await api('/auth/key-login','POST',{access_key:byId('access-key').value});byId('access-key').value='';await load();if(byId('login').hidden){clearInterval(timer);timer=setInterval(load,15000);}}catch(error){byId('access-key-error').textContent=error.message;}finally{button.disabled=false;}});
  byId('setup-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;byId('setup-error').textContent='';try{await api('/auth/setup','POST',{setup_key:byId('setup-key-field').hidden?'':byId('setup-key').value,username:byId('setup-username').value,password:byId('setup-password').value,display_name:byId('setup-display-name').value,organization:byId('setup-organization').value,project:byId('setup-project').value,description:byId('setup-description').value});byId('setup-key').value='';byId('setup-password').value='';await load();if(byId('login').hidden){clearInterval(timer);timer=setInterval(load,15000);}}catch(error){byId('setup-error').textContent=error.message;}finally{button.disabled=false;}});
  byId('signup-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;byId('signup-error').textContent='';try{await api('/auth/signup','POST',{username:byId('signup-username').value,password:byId('signup-password').value,workspace_name:byId('signup-workspace').value,display_name:byId('signup-display-name').value,organization:byId('signup-organization').value,project:byId('signup-project').value,description:byId('signup-description').value});byId('signup-password').value='';await load();if(byId('login').hidden){clearInterval(timer);timer=setInterval(load,15000);}}catch(error){byId('signup-error').textContent=error.message;}finally{button.disabled=false;}});
  byId('logout').addEventListener('click',async()=>{try{await api('/auth/logout','POST',{});}catch{}agentRegistrationSecrets.clear();newAgentAccess=null;rotationPendingAgent=null;byId('rotated-agent-token').value='';for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('agent-mailbox-rotation:'))sessionStorage.removeItem(key);}view=null;await showLogin();});
  let resizeFrame;new ResizeObserver(()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>view&&renderMessages(view.messages,view.agents,view.permissions));}).observe(byId('map'));
  load().then(()=>{if(byId('login').hidden)timer=setInterval(load,15000);});
})();
