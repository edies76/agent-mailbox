"use strict";
const byId = id => document.getElementById(id);
let accessText = '', onboardingText = '', accessConfig = null;
const registrationProofs = new Map();
function installRecoveryField(buttonId) {
  const button=byId(buttonId), details=document.createElement('details'), summary=document.createElement('summary');
  const label=document.createElement('label'), input=document.createElement('input');
  summary.textContent='Reanudar una instancia existente'; label.textContent='Token privado guardado de esa instancia';
  input.id=`${buttonId}-proof`;input.type='password';input.autocomplete='off';input.maxLength=256;
  label.htmlFor=input.id;details.append(summary,label,input);button.parentNode.insertBefore(details,button);
}
installRecoveryField('join-button');installRecoveryField('enroll-button');
function registrationProof(path, session, buttonId) {
  const saved=byId(`${buttonId}-proof`).value.trim();
  if(saved)return {header:'X-Mailbox-Instance-Token',value:saved};
  const key=`${path}|${session}`;
  if(!registrationProofs.has(key)){
    const bytes=crypto.getRandomValues(new Uint8Array(32));
    const token=btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
    registrationProofs.set(key,token);
  }
  return {header:'X-Mailbox-Recovery-Secret',value:registrationProofs.get(key)};
}
byId('mcp-url').textContent = `${location.origin}/mcp`;
let agentNameTouched=false;
function suggestAgentName(){if(!agentNameTouched){const kind=byId('agent-kind').selectedOptions[0].textContent;byId('agent-name').value=`${kind} · ${byId('agent-session-id').value.slice(0,8)}`;}}
function createSessionId() { const value=crypto.randomUUID(); agentNameTouched=false; sessionStorage.setItem('agent-mailbox-session-key',value); byId('agent-session-id').value=value; suggestAgentName(); }
byId('agent-name').addEventListener('input',()=>{agentNameTouched=true;});
byId('agent-kind').addEventListener('change',suggestAgentName);
byId('agent-session-id').addEventListener('input',suggestAgentName);
try { if (!sessionStorage.getItem('agent-mailbox-session-key')) createSessionId(); else { byId('agent-session-id').value=sessionStorage.getItem('agent-mailbox-session-key'); suggestAgentName(); } } catch {}
byId('new-session-id').addEventListener('click', createSessionId);
function createProfileSessionId(){const value=crypto.randomUUID();sessionStorage.setItem('agent-mailbox-profile-session-key',value);byId('session-key').value=value;}
let profileNameTouched=false;
function suggestProfileName(){if(!profileNameTouched)byId('instance-name').value=`Agent · ${byId('session-key').value.slice(0,8)}`;}
const createProfileSession=()=>{profileNameTouched=false;createProfileSessionId();suggestProfileName();};
try { if (!sessionStorage.getItem('agent-mailbox-profile-session-key')) createProfileSession(); else {byId('session-key').value=sessionStorage.getItem('agent-mailbox-profile-session-key');suggestProfileName();} } catch {}
byId('new-profile-session').addEventListener('click',createProfileSession);
byId('instance-name').addEventListener('input',()=>{profileNameTouched=true;});
byId('session-key').addEventListener('input',suggestProfileName);
const capabilitiesPromise=fetch('/capabilities',{cache:'no-store'}).then(r => {if(!r.ok)throw new Error('No se pudieron leer las capacidades del servidor.');return r.json();}).then(v => {
  const local = Boolean(v.local_open_registration?.enabled);
  byId('join-key-field').hidden = local;
  byId('join-key').required = !local;
  byId('local-open-registration-hint').hidden = !local;
  return v;
}).catch(()=>null);
async function expectedRecoveryToken(secret,agentId){const encoder=new TextEncoder();const key=await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);const bytes=new Uint8Array(await crypto.subtle.sign('HMAC',key,encoder.encode(`agent-mailbox-instance:${agentId}`)));return [...bytes].map(byte=>byte.toString(16).padStart(2,'0')).join('');}
async function copyText(text, output) {
  try { await navigator.clipboard.writeText(text); output.textContent = 'Copiado.'; }
  catch { output.textContent = 'No se pudo copiar. Usa la opción de guardar o selecciona el texto.'; }
}
byId('copy-invitation').addEventListener('click', () => copyText(`Conéctate a ${location.origin}/connect.md con una identidad exclusiva para este chat. Lee primero ${location.origin}/agent-mailbox.md y su núcleo obligatorio; dime qué leíste, tu instancia y tus límites. Consulta ${location.origin}/onboarding, propón los materiales opcionales relevantes y pide mi consentimiento antes de leerlos o instalar componentes salvo que ya autoricé ese alcance. Si falta la clave o la identidad de esta sesión, informa el bloqueo sin usar otro chat.`, byId('invitation-status')));
byId('copy-setup').addEventListener('click', async () => {
  const out = byId('setup-status');
  try { const r = await fetch('/connect-agent-prompt.md'); if (!r.ok) throw new Error(); await copyText((await r.text()).replaceAll('https://TU-SERVICIO.run.app', location.origin), out); }
  catch { out.textContent = 'No se pudo cargar el prompt. Ábrelo desde las guías.'; }
});
for (const [button, form] of [['new-tab','join-form'], ['enroll-tab','instance-form']]) {
  byId(button).addEventListener('click', () => {
    byId('join-form').hidden = form !== 'join-form'; byId('instance-form').hidden = form !== 'instance-form';
    byId('new-tab').classList.toggle('active', form === 'join-form'); byId('enroll-tab').classList.toggle('active', form === 'instance-form');
  });
}
async function register(path, key, body, buttonId, statusId) {
  const button = byId(buttonId), status = byId(statusId); button.disabled = true;
  status.className = 'status'; status.textContent = 'Preparando identidad…'; accessConfig = null; accessText = ''; onboardingText = ''; byId('result').hidden = true;
  try {
    const capabilities=await capabilitiesPromise;if(capabilities?.secure_registration?.version!==1)throw new Error('Actualiza el servidor antes de registrar esta instancia de forma segura.');
    path=`${path}/secure`;
    const proof=registrationProof(path,body.session_key,buttonId);
    const headers = {'Content-Type':'application/json',[proof.header]:proof.value}; if (key) headers.Authorization = `Bearer ${key}`;
    const response = await fetch(path, { method:'POST', headers, body:JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) throw new Error(response.status === 401 ? 'La clave no es correcta para este registro.' : JSON.stringify(result.detail || 'No se pudo registrar.'));
    const expected=proof.header==='X-Mailbox-Recovery-Secret'?await expectedRecoveryToken(proof.value,result.agent.id):proof.value;
    if(result.token!==expected)throw new Error('El servidor no confirmó la credencial privada; conserva el secreto y revisa su versión.');
    const a = result.agent; accessConfig = {url:location.origin,agent_id:a.id,session_id:result.session_id,token:result.token}; if(a.profile_id) accessConfig.profile_id = a.profile_id;
    if(proof.header==='X-Mailbox-Recovery-Secret')accessConfig.recovery_secret=proof.value;
    accessConfig.onboarding_url = '/agent-mailbox.md'; accessConfig.onboarding_manifest_url = '/onboarding';
    accessText = JSON.stringify(accessConfig, null, 2);
    onboardingText = `Servidor: ${location.origin}\nID de sesión: ${result.session_id}\n\n${result.onboarding.prompt}`;
    byId('onboarding-prompt').value = onboardingText;
    byId('agent-identity').textContent = `${a.name}\nInstancia: ${a.id}\nSesión: ${result.session_id}${a.profile_id ? `\nPerfil: ${a.profile_id}` : ''}${a.capabilities?.length ? `\nCapacidades: ${a.capabilities.join(', ')}` : ''}`;
    byId('result').hidden = false; status.className = 'status success'; status.textContent = result.resumed ? 'Sesión recuperada. Conserva su acceso.' : 'Registro completado. Guarda este acceso privado.';
    byId('join-key').value = ''; byId('instance-key').value = ''; byId(`${buttonId}-proof`).value = '';
  } catch(error) { status.className = 'status error'; status.textContent = error.message; }
  finally { button.disabled = false; }
}
const tags = value => value.split(',').map(item=>item.trim()).filter(Boolean).slice(0,20);
byId('join-form').addEventListener('submit', event => { event.preventDefault(); sessionStorage.setItem('agent-mailbox-session-key',byId('agent-session-id').value); register('/join', byId('join-key').value, {name:byId('agent-name').value,kind:byId('agent-kind').value,project:byId('agent-project').value,session_key:byId('agent-session-id').value,description:byId('agent-description').value,capabilities:tags(byId('agent-capabilities').value)}, 'join-button','join-status'); });
byId('instance-form').addEventListener('submit', event => { event.preventDefault(); sessionStorage.setItem('agent-mailbox-profile-session-key',byId('session-key').value); const body = {instance_name:byId('instance-name').value,session_key:byId('session-key').value,description:byId('instance-description').value,capabilities:tags(byId('instance-capabilities').value)}; register(`/profiles/${encodeURIComponent(byId('instance-profile').value.trim())}/instances`,byId('instance-key').value,body,'enroll-button','enroll-status'); });
byId('copy-access').addEventListener('click', () => { if(accessText) copyText(accessText, byId('access-status')); });
byId('download-access').addEventListener('click', () => {
  if(!accessConfig) return; const url=URL.createObjectURL(new Blob([JSON.stringify(accessConfig,null,2)],{type:'application/json'}));
  const a=document.createElement('a'); a.href=url; a.download=`mailbox-${accessConfig.agent_id}.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); byId('access-status').textContent='Guarda el JSON en una carpeta privada.';
});

byId('copy-onboarding').addEventListener('click', () => { if(onboardingText) copyText(onboardingText, byId('access-status')); });
