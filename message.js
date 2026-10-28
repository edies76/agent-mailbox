/* Ruta de un mensaje del Control Room: modo de entrega, despertar, hilo y participantes. */
(() => {
  const byId = (id) => document.getElementById(id);
  const messageId = (location.pathname.match(/\/dashboard\/message\/(\d+)/) || [])[1];

  const fmtTime = (value) => value ? new Date(value * 1000).toLocaleString('es', {dateStyle: 'medium', timeStyle: 'short'}) : '—';
  const fmtRelative = (value) => {
    if (!value) return 'sin datos';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - value));
    if (seconds < 90) return 'hace instantes';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `hace ${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `hace ${hours} h`;
    return `hace ${Math.floor(hours / 24)} d`;
  };
  const fmtBytes = (value) => {
    if (!Number.isFinite(value)) return '—';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const modeLabel = (mode) => (mode === 'wake' ? 'Despertar' : 'Normal');
  const stateLabel = (wake) => ({
    sin_despertar: 'No pedía despertar',
    solicitado: 'Despertar solicitado',
    atendido: 'Despertar atendido',
  }[wake.state] || wake.state);
  const stateClass = (wake) => (wake.mode !== 'wake' ? 'inbox'
    : wake.state === 'atendido' ? 'wake-atendido'
    : wake.state === 'solicitado' ? 'wake-solicitado' : 'inbox');

  async function api(path) {
    const response = await fetch(path, {credentials: 'same-origin', headers: {Accept: 'application/json'}});
    if (response.status === 401 || response.status === 403) {
      const error = new Error('La sesión del panel no está activa.');
      error.status = response.status;
      throw error;
    }
    if (response.status === 404) {
      const error = new Error('Ese mensaje no existe en este espacio.');
      error.status = 404;
      throw error;
    }
    if (!response.ok) throw new Error(`El servidor respondió ${response.status}.`);
    return response.json();
  }

  function renderBadges(message, wake) {
    const row = byId('detail-badges');
    row.replaceChildren();
    row.append(el('span', `env ${message.delivery_mode === 'wake' ? 'wake-solicitado' : 'inbox'}`,
                  `Entrega · ${modeLabel(message.delivery_mode)}`));
    row.append(el('span', `env ${stateClass(wake)}`, stateLabel(wake)));
    row.append(el('span', `env ${message.acknowledged_at ? 'cerrado' : ''}`,
                  message.acknowledged_at ? 'Cerrado' : 'Pendiente'));
    if (message.wake_backlog) row.append(el('span', 'env', 'Lee pendientes anteriores'));
    const attachments = message.attachment_count || 0;
    if (attachments) row.append(el('span', 'env', `${attachments} adjunto${attachments === 1 ? '' : 's'}`));
  }

  function renderFacts(message, wake) {
    const facts = byId('detail-facts');
    facts.replaceChildren();
    const rows = [
      ['Mensaje', `#${message.id}`, true],
      ['Hilo', `${message.thread_id ?? '—'}`, true],
      ['Responde a', message.in_reply_to ? `#${message.in_reply_to}` : 'No es una respuesta', true],
      ['Tipo', message.message_type || '—'],
      ['Modo de entrega', modeLabel(message.delivery_mode)],
      ['Despertar', wake.label],
      ['Evidencia del despertar', wake.evidence === 'respondido' ? 'Respondido por el destinatario'
        : wake.evidence === 'acuse' ? 'Acuse del destinatario'
        : wake.mode === 'inbox' ? 'No aplica' : 'Sin evidencia todavía'],
      ['Estado', message.status || '—'],
      ['Huella de la petición', message.request_fingerprint || '—', true],
      ['Creado', fmtTime(message.created_at)],
      ['Confirmado', message.acknowledged_at ? fmtTime(message.acknowledged_at) : 'Sin acuse'],
      ['Adjuntos', `${message.attachment_count || 0}${message.has_images ? ' · con imágenes' : ''}${message.has_files ? ' · con archivos' : ''}`],
    ];
    for (const [label, value, mono] of rows) {
      const cell = el('div');
      cell.append(el('dt', '', label));
      cell.append(el('dd', mono ? 'mono' : '', value));
      facts.append(cell);
    }
  }

  function renderBody(message, data) {
    const body = byId('detail-body');
    const hint = byId('detail-content-hint');
    body.replaceChildren();
    if (!data.content_visible) {
      hint.textContent = 'Tu rol no puede leer el contenido de los mensajes.';
      body.append(el('p', 'body-hidden', 'Este rol ve la ruta, el estado y los metadatos, no el texto ni los archivos.'));
      return;
    }
    hint.textContent = 'Texto íntegro tal como lo recibió el destinatario.';
    body.append(el('p', 'body-text', message.text || '(mensaje sin texto)'));
    const attachments = Array.isArray(message.attachments) ? message.attachments : [];
    if (!attachments.length) return;
    const list = el('ul', 'attach');
    for (const item of attachments) {
      const row = el('li');
      row.append(el('span', '', item.filename || `adjunto ${item.index ?? ''}`));
      row.append(el('span', 'mono', `${item.media_type || 'archivo'} · ${fmtBytes(item.size_bytes)}`));
      list.append(row);
    }
    const wrap = byId('detail-attachments');
    wrap.replaceChildren(el('p', 'hint', 'Adjuntos guardados con el mensaje (metadatos; el contenido se descarga desde la API).'), list);
  }

  function renderPeople(participants, message) {
    const wrap = byId('detail-people');
    wrap.replaceChildren();
    for (const role of ['from', 'to']) {
      const person = participants[role];
      const block = el('div', 'person');
      const label = role === 'from' ? 'Origen' : 'Destino';
      block.append(el('span', 'meta', label));
      if (!person) {
        block.append(el('strong', '', role === 'from' ? message.from_agent_id : message.to_agent_id));
        block.append(el('span', 'meta', 'La instancia ya no está registrada'));
      } else {
        block.append(el('strong', '', person.instance_name || person.name || person.id));
        block.append(el('span', 'meta', [person.kind, person.project].filter(Boolean).join(' · ') || 'sin proyecto declarado'));
        if (person.chat_path) block.append(el('span', 'meta', person.chat_path));
        block.append(el('span', 'meta', `${person.online ? 'en línea' : 'inactiva'} · visto ${fmtRelative(person.last_seen)}`));
        block.append(el('span', 'meta', person.id));
      }
      const divider = el('div');
      divider.style.margin = '14px 0';
      divider.style.borderTop = '1px solid var(--line)';
      wrap.append(block, divider);
    }
    wrap.lastChild.remove();
  }

  function renderThread(thread, currentId, names) {
    const list = byId('detail-thread');
    const hint = byId('detail-thread-hint');
    list.replaceChildren();
    hint.textContent = `${thread.length} mensaje${thread.length === 1 ? '' : 's'} en este hilo, del más antiguo al más nuevo.`;
    for (const entry of thread) {
      const item = el('li');
      const link = el('a');
      link.href = `/dashboard/message/${entry.id}`;
      const head = el('div');
      head.append(el('time', '', fmtTime(entry.created_at)));
      if (entry.id === Number(currentId)) head.append(el('span', 'current', ' · este mensaje'));
      link.append(head);
      const route = el('strong', '', `${names.get(entry.from_agent_id) || entry.from_agent_id} → ${names.get(entry.to_agent_id) || entry.to_agent_id}`);
      link.append(route);
      link.append(el('span', 'subject', `${entry.delivery_mode === 'wake' ? 'Despertar · ' : ''}${entry.subject || 'Sin asunto'}`));
      item.append(link);
      list.append(item);
    }
  }

  function showNotice(text) {
    const notice = byId('detail-notice');
    notice.hidden = false;
    notice.replaceChildren(el('strong', '', text));
  }

  async function load() {
    const data = await api(`/dashboard/messages/${messageId}`);
    const message = data.message;
    const names = new Map();
    if (data.participants.from) names.set(data.participants.from.id, data.participants.from.instance_name || data.participants.from.name);
    if (data.participants.to) names.set(data.participants.to.id, data.participants.to.instance_name || data.participants.to.name);
    const from = names.get(message.from_agent_id) || message.from_agent_id;
    const to = names.get(message.to_agent_id) || message.to_agent_id;
    byId('detail-eyebrow').textContent = `Mensaje #${message.id} · hilo ${message.thread_id ?? '—'} · ${message.message_type || 'mensaje'}`;
    const title = byId('detail-title');
    title.replaceChildren(document.createTextNode(from), el('span', 'arrow', '→'), document.createTextNode(to));
    renderBadges(message, data.wake);
    renderFacts(message, data.wake);
    renderBody(message, data);
    renderPeople(data.participants, message);
    renderThread(data.thread || [], message.id, names);
    byId('detail-updated').textContent = `Actualizado ${new Date(data.checked_at * 1000).toLocaleTimeString('es')}`;
    document.title = `Mensaje #${message.id} · Agent Mailbox`;
  }

  async function refresh() {
    try {
      await load();
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        location.href = '/dashboard';
        return;
      }
      showNotice(error.message);
    }
  }

  if (!messageId) {
    showNotice('Falta el identificador del mensaje en la dirección.');
  } else {
    refresh();
    setInterval(refresh, 15000);
  }
})();
