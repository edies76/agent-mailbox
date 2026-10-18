(() => {
  const byId = id => document.getElementById(id);
  const keyName = 'agent-mailbox-dashboard-key';
  let adminKey = sessionStorage.getItem(keyName) || '';
  let timer;

  function confirmAction(description) {
    return new Promise(resolve => {
      const dialog=byId('admin-confirm'); byId('confirm-description').textContent=description;
      byId('confirm-yes').onclick=()=>{dialog.close();resolve(true);};
      byId('confirm-no').onclick=()=>{dialog.close();resolve(false);};
      dialog.oncancel=()=>resolve(false); dialog.showModal();
    });
  }
  function fmtTime(value) {
    return new Date(value * 1000).toLocaleString('es-CO', { day:'2-digit', month:'short', hour: '2-digit', minute: '2-digit' });
  }
  function setText(id, value) { byId(id).textContent = String(value); }
  async function load() {
    try {
      const response = await fetch('/dashboard/data', { headers: { Authorization: `Bearer ${adminKey}` }, cache: 'no-store' });
      if (response.status === 401 || response.status === 403) {
        sessionStorage.removeItem(keyName); adminKey = ''; clearInterval(timer);
        byId('login').hidden = false; setText('connection', 'CLAVE REQUERIDA'); return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      render(data); byId('login').hidden = true; setText('connection', 'EN VIVO');
    } catch (error) {
      setText('connection', 'SIN CONEXIÓN');
    }
  }
  function render(data) {
    const { stats, agents, messages } = data;
    setText('total', stats.agents_total); setText('online', stats.agents_online);
    setText('messages', stats.messages_total); setText('pending', stats.messages_pending);
    setText('agent-count', `${stats.agents_online} en línea · ${agents.length} total`);
    setText('updated', `actualizado ${fmtTime(data.updated_at)}`);
    renderAgents(agents); renderMessages(messages, agents);
  }
  function renderAgents(agents) {
    const list = byId('agent-list'); list.replaceChildren();
    if (!agents.length) { list.innerHTML = '<div class="empty">Todavía no se ha registrado ningún agente.</div>'; return; }
    for (const agent of agents) {
      const row = document.createElement('div'); row.className = 'agent';
      const info = document.createElement('div');
      const name = document.createElement('div'); name.className = 'agent-name'; name.textContent = agent.name;
      const kind = document.createElement('div'); kind.className = 'agent-kind'; kind.textContent = `${agent.kind}${agent.project ? ` · ${agent.project}` : ''}`;
      info.append(name, kind);
      const badge = document.createElement('span'); badge.className = `badge${agent.online ? ' on' : ''}`; badge.textContent = agent.online ? 'EN LÍNEA' : 'INACTIVO';
      const actions = document.createElement('div'); actions.className = 'agent-actions';
      const remove = document.createElement('button'); remove.className = 'remove-agent'; remove.type = 'button';
      remove.textContent = 'Dar de baja'; remove.setAttribute('aria-label', `Dar de baja a ${agent.name}`);
      remove.addEventListener('click', () => unregisterAgent(agent));
      actions.append(badge, remove); row.append(info, actions); list.append(row);
    }
  }
  async function unregisterAgent(agent) {
    const confirmed = await confirmAction(`Dar de baja a ${agent.name}. Esto revoca su token y borra permanentemente todos sus mensajes y archivos adjuntos.`);
    if (!confirmed) return;
    try {
      const response = await fetch(`/agents/${encodeURIComponent(agent.id)}`, {
        method: 'DELETE', headers: { Authorization: `Bearer ${adminKey}` }, cache: 'no-store'
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await load();
    } catch (error) {
      byId('admin-error').textContent = `No se pudo dar de baja a ${agent.name}: ${error.message}`;
    }
  }
  function renderMessages(messages, agents) {
    const nameById = new Map(agents.map(a => [a.id, a.name]));
    const feed = byId('feed'); feed.replaceChildren();
    const map = byId('map'); const nodes = byId('nodes'); const edgeList = byId('edge-list');
    nodes.replaceChildren(); edgeList.replaceChildren();
    const empty = byId('map-empty'); empty.hidden = messages.length > 0;
    if (!agents.length || !messages.length) {
      feed.innerHTML = '<div class="empty">Los mensajes nuevos aparecerán aquí.</div>'; return;
    }
    const width = map.clientWidth || 800, height = map.clientHeight || 380;
    const locations = new Map();
    agents.forEach((agent, index) => {
      const angle = -Math.PI / 2 + 2 * Math.PI * index / agents.length;
      const x = agents.length === 1 ? .5 : .5 + .35 * Math.cos(angle);
      const y = agents.length === 1 ? .5 : .5 + .34 * Math.sin(angle);
      locations.set(agent.id, [x * 800, y * 380]);
      const node = document.createElement('div'); node.className = `node${agent.online ? ' online' : ''}`;
      node.style.left = `${x * width}px`; node.style.top = `${y * height}px`;
      const title = document.createElement('div'); title.className = 'node-name'; title.textContent = agent.name;
      const meta = document.createElement('div'); meta.className = 'node-meta'; meta.textContent = agent.kind;
      node.append(title, meta); nodes.append(node);
    });
    for (const message of messages.slice(0, 8).reverse()) {
      const start = locations.get(message.from_agent_id), end = locations.get(message.to_agent_id);
      if (start && end && message.from_agent_id !== message.to_agent_id) {
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', start[0]); line.setAttribute('y1', start[1]);
        line.setAttribute('x2', end[0]); line.setAttribute('y2', end[1]);
        line.setAttribute('marker-end', 'url(#arrow)'); line.setAttribute('class', 'edge'); edgeList.append(line);
      }
    }
    for (const message of messages) {
      const event = document.createElement('article'); event.className = 'event';
      const time = document.createElement('time'); time.className = 'event-time'; time.textContent = fmtTime(message.created_at);
      const main = document.createElement('div'); main.className = 'event-main';
      const route = document.createElement('div'); route.className = 'route';
      const from = document.createElement('span'); from.textContent = nameById.get(message.from_agent_id) || message.from_agent_id;
      const arrow = document.createElement('span'); arrow.className = 'arrow'; arrow.textContent = '→';
      const to = document.createElement('span'); to.textContent = nameById.get(message.to_agent_id) || message.to_agent_id;
      route.append(from, arrow, to);
      const content = document.createElement('div'); content.className = 'event-text';
      const attachmentCount = (message.attachments || []).length;
      content.textContent = [message.text, attachmentCount ? `📎 ${attachmentCount} archivo${attachmentCount === 1 ? '' : 's'} adjunto${attachmentCount === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
      const state = document.createElement('div'); state.className = 'event-state'; state.textContent = message.acknowledged_at ? `${message.status} · hilo ${message.thread_id} · mensaje ${message.id}` : `${message.status} · pendiente · hilo ${message.thread_id} · mensaje ${message.id}`;
      const subject=document.createElement('div'); subject.className='event-subject'; subject.textContent=message.subject || 'Sin asunto'; main.append(route, subject, content, state);
      const remove = document.createElement('button'); remove.className = 'remove-message'; remove.type = 'button';
      remove.textContent = 'Eliminar'; remove.setAttribute('aria-label', `Eliminar mensaje ${message.id}`);
      remove.addEventListener('click', () => deleteMessage(message));
      event.append(time, main, remove); feed.append(event);
    }
  }
  async function deleteMessage(message) {
    if (!await confirmAction(`Eliminar el mensaje ${message.id} para ambos agentes y borrar sus archivos adjuntos.`)) return;
    try {
      const response = await fetch(`/messages/${encodeURIComponent(message.id)}`, {
        method: 'DELETE', headers: { Authorization: `Bearer ${adminKey}` }, cache: 'no-store'
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await load();
    } catch (error) {
      byId('admin-error').textContent = `No se pudo eliminar el mensaje ${message.id}: ${error.message}`;
    }
  }
  byId('login-form').addEventListener('submit', async event => {
    event.preventDefault(); byId('login-error').textContent = '';
    adminKey = byId('key').value.trim(); sessionStorage.setItem(keyName, adminKey); byId('key').value = '';
    await load();
    if (byId('login').hidden) { timer = setInterval(load, 15000); }
    else byId('login-error').textContent = 'Esa clave no abrió el panel. Revisa que sea la clave administrativa.';
  });
  byId('logout').addEventListener('click', () => {
    clearInterval(timer); sessionStorage.removeItem(keyName); adminKey = '';
    byId('login').hidden = false; setText('connection', 'CLAVE REQUERIDA');
  });
  if (adminKey) load().then(() => { if (byId('login').hidden) timer = setInterval(load, 15000); });
  else byId('login').hidden = false;
})();

