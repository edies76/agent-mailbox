"use strict";
const byId = id => document.getElementById(id);
let accessText = '', accessConfig = null;
byId('mcp-url').textContent = `${location.origin}/mcp`;
fetch('/health').then(r => r.json()).then(v => document.querySelectorAll('[data-version]').forEach(el => el.textContent = `v${v.version}`)).catch(() => {});
async function copyText(text, output) {
  try { await navigator.clipboard.writeText(text); output.textContent = 'Copiado.'; }
  catch { output.textContent = 'No se pudo copiar. Usa la opción de guardar o selecciona el texto.'; }
}
byId('copy-invitation').addEventListener('click', () => copyText(`Conéctate a ${location.origin}/connect.md. Usa una identidad exclusiva para esta sesión; si te falta la clave de acceso pídemela. Carga la skill ${location.origin}/skill.md y las instrucciones persistentes ${location.origin}/guide/instructions. Revisa el buzón al inicio y antes de terminar una tarea.`, byId('invitation-status')));
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
  status.className = 'status'; status.textContent = 'Preparando identidad…'; accessConfig = null; accessText = ''; byId('result').hidden = true;
  try {
    const response = await fetch(path, { method:'POST', headers:{'Content-Type':'application/json',Authorization:`Bearer ${key}`},body:JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) throw new Error(response.status === 401 ? 'La clave no es correcta para este registro.' : JSON.stringify(result.detail || 'No se pudo registrar.'));
    const a = result.agent; accessConfig = {url:location.origin,agent_id:a.id,token:result.token}; if(a.profile_id) accessConfig.profile_id = a.profile_id;
    accessText = JSON.stringify(accessConfig, null, 2) + `\n\nInstancia: ${a.name}. Usa esta identidad solo en este chat. Lee primero ${location.origin}/agent-bank.md, luego ${location.origin}/connect.md y ${location.origin}/skill.md. Publica tus herramientas reales con mailbox_update_card y busca colegas relevantes con mailbox_discover. Las respuestas se envían con ID y request_fingerprint del mensaje original. Revisa todos los pendientes al inicio y al final de tu tarea.`;
    byId('agent-identity').textContent = `${a.name}\nInstancia: ${a.id}${a.profile_id ? `\nPerfil: ${a.profile_id}` : ''}${a.capabilities?.length ? `\nCapacidades: ${a.capabilities.join(', ')}` : ''}`;
    byId('result').hidden = false; status.className = 'status success'; status.textContent = result.resumed ? 'Sesión recuperada. Conserva su acceso.' : 'Registro completado. Guarda este acceso privado.';
    byId('join-key').value = ''; byId('instance-key').value = '';
  } catch(error) { status.className = 'status error'; status.textContent = error.message; }
  finally { button.disabled = false; }
}
const tags = value => value.split(',').map(item=>item.trim()).filter(Boolean).slice(0,20);
byId('join-form').addEventListener('submit', event => { event.preventDefault(); register('/join', byId('join-key').value, {name:byId('agent-name').value,kind:byId('agent-kind').value,project:byId('agent-project').value,description:byId('agent-description').value,capabilities:tags(byId('agent-capabilities').value)}, 'join-button','join-status'); });
byId('instance-form').addEventListener('submit', event => { event.preventDefault(); const body = {instance_name:byId('instance-name').value,description:byId('instance-description').value,capabilities:tags(byId('instance-capabilities').value)}; if(byId('session-key').value) body.session_key=byId('session-key').value; register(`/profiles/${encodeURIComponent(byId('instance-profile').value.trim())}/instances`,byId('instance-key').value,body,'enroll-button','enroll-status'); });
byId('copy-access').addEventListener('click', () => { if(accessText) copyText(accessText, byId('access-status')); });
byId('download-access').addEventListener('click', () => {
  if(!accessConfig) return; const url=URL.createObjectURL(new Blob([JSON.stringify(accessConfig,null,2)],{type:'application/json'}));
  const a=document.createElement('a'); a.href=url; a.download=`mailbox-${accessConfig.agent_id}.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); byId('access-status').textContent='Guarda el JSON en una carpeta privada.';
});
