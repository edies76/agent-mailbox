(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {items: [], claims: [], selected: null, revision: null, previewUrl: null, listKey: '', activeLoad: 0, accountId: null, edit: null, displayedText: null};
  const node = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; };
  const formatTime = value => { if (!value) return 'Fecha no disponible'; const date = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(date.getTime()) ? 'Fecha no disponible' : date.toLocaleString('es-CO', {dateStyle:'medium', timeStyle:'short'}); };
  const csrf = () => { const cookie = document.cookie.split('; ').find(v => v.startsWith('__Host-agent-mailbox-csrf=') || v.startsWith('agent-mailbox-csrf=')); return cookie ? decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)) : ''; };
  async function api(path, method='GET', body) {
    const headers = {}; if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (!['GET','HEAD'].includes(method)) headers['X-CSRF-Token'] = csrf();
    const res = await fetch(path, {method, headers, credentials:'same-origin', cache:'no-store', body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { if (res.status === 401) location.href = '/dashboard'; const error = new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`); error.status = res.status; throw error; }
    return data;
  }
  function bytesToBase64(bytes) { let binary=''; for (let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192)); return btoa(binary); }
  async function payload(fileInput, textInput) {
    const file = fileInput.files[0];
    const bytes = file ? new Uint8Array(await file.arrayBuffer()) : new TextEncoder().encode(textInput.value);
    if (bytes.length > 5 * 1024 * 1024) throw new Error('La versión no puede superar 5 MiB.');
    if (!bytes.length) throw new Error('Añade contenido de texto o selecciona un archivo.');
    return {content_type: file ? (file.type || 'application/octet-stream') : 'text/plain', data_base64: bytesToBase64(bytes)};
  }
  function renderList() {
    const q = $('search').value.trim().toLocaleLowerCase();
    const items = state.items.filter(item => `${item.title || ''} ${item.path || ''}`.toLocaleLowerCase().includes(q));
    $('item-count').textContent = `${state.items.length} ${state.items.length === 1 ? 'recurso' : 'recursos'}`;
    const key = JSON.stringify([state.selected, q, items.map(item => [item.id,item.title,item.path,item.revision,item.updated_at])]);
    if (key === state.listKey) return; state.listKey = key; $('item-list').replaceChildren();
    if (!items.length) { $('item-list').append(node('div','loading',state.items.length ? 'No hay resultados para esa búsqueda.' : 'Aún no hay recursos. Añade el primero.')); return; }
    for (const item of items) { const button=node('button','item');button.type='button';button.setAttribute('aria-current',String(item.id === state.selected));button.append(node('strong','',item.title),node('small','',`${item.path} · versión ${item.revision}`));button.onclick=()=>openItem(item.id);$('item-list').append(button); }
  }
  function renderClaims() {
    const list=$('claim-list');list.replaceChildren();
    if (!state.claims.length) { list.append(node('span','muted','No hay rutas reservadas.'));return; }
    for (const item of state.claims) { const entry=node('div','claim');const mine=item.owner_id===`account:${state.accountId}`;entry.append(node('strong','',item.path),node('small','',`Por ${mine?'ti':item.owner_id} · hasta ${formatTime(item.expires_at)}`));if(mine){const release=node('button','text-button','Liberar mi reserva');release.type='button';release.onclick=async()=>{try{await api(`/context/claims?path=${encodeURIComponent(item.path)}`,'DELETE');await refresh();}catch(error){$('global-status').textContent=error.message;}};entry.append(release);}list.append(entry); }
  }
  async function refresh() {
    try { const [items,claims]=await Promise.all([api('/context/items'),api('/context/claims')]);state.items=items.items;state.claims=claims.claims;renderList();renderClaims(); }
    catch(error) { $('global-status').textContent=error.status===501?'El almacenamiento de contexto aún no está disponible en este servidor.':`No se pudo cargar el contexto: ${error.message}`; }
  }
  function clearPreview() { if(state.previewUrl) URL.revokeObjectURL(state.previewUrl);state.previewUrl=null; }
  async function showVersion(item, revision, preview, versionsRoot) {
    const serial=++state.activeLoad;preview.textContent='Cargando versión…';
    try { const result=await api(`/context/items/${encodeURIComponent(item.id)}/versions/${encodeURIComponent(revision)}`); if(serial!==state.activeLoad)return;
      clearPreview();const bytes=Uint8Array.from(atob(result.data_base64),c=>c.charCodeAt(0));const type=result.version?.content_type || item.content_type || 'application/octet-stream';preview.replaceChildren();state.displayedText=null;
      if (/^(text\/|application\/(json|xml|x-ndjson))/.test(type)) {state.displayedText=new TextDecoder().decode(bytes);preview.textContent=state.displayedText;}
      else if (/^image\/(png|jpeg|gif|webp)$/.test(type)) { const img=document.createElement('img');state.previewUrl=URL.createObjectURL(new Blob([bytes],{type}));img.src=state.previewUrl;img.alt=`Vista previa de ${item.title}`;preview.append(img); }
      else {const blob=new Blob([bytes],{type:'application/octet-stream'});state.previewUrl=URL.createObjectURL(blob);const a=node('a','',`Descargar ${item.title}`);a.href=state.previewUrl;a.download=item.path.split('/').pop()||'archivo';preview.append(a);}
      state.revision=revision;versionsRoot.querySelectorAll('button').forEach(b=>b.setAttribute('aria-current',String(b.dataset.revision===String(revision))));
    }catch(error){if(serial===state.activeLoad)preview.textContent=`No se pudo cargar esta versión: ${error.message}`;}
  }
  async function openItem(id) {
    state.selected=id;renderList();const reader=$('reader');reader.replaceChildren(node('div','loading','Cargando recurso…'));
    try {const [result,history]=await Promise.all([api(`/context/items/${encodeURIComponent(id)}`),api(`/context/items/${encodeURIComponent(id)}/versions`)]);if(state.selected!==id)return;
      const item=result.item;const head=node('div','reader-head');const info=node('div');info.append(node('span','eyebrow','CONTEXTO COMPARTIDO'),node('h2','',item.title),node('p','',`${item.path} · versión actual ${item.revision} · actualizado ${formatTime(item.updated_at)}`));
      const actions=node('div','reader-actions');
      if(/\.(jsonl|ndjson)$/i.test(item.path)){const append=node('button','button secondary','Registrar avance');append.type='button';append.onclick=()=>{$('jsonl-form').reset();state.edit={id:item.id,revision:item.revision};$('jsonl-error').textContent='';$('jsonl-for').textContent=`${item.title} · se añadirá al historial después de la revisión ${item.revision}.`;dialog('jsonl-dialog');};actions.append(append);}
      const edit=node('button','button secondary','Nueva versión');edit.type='button';edit.onclick=()=>{$('revision-form').reset();state.edit={id:item.id,revision:item.revision};if(state.revision===item.revision&&state.displayedText!==null)$('revision-content').value=state.displayedText;$('revision-error').textContent='';$('revision-for').textContent=`${item.title} · versión actual ${item.revision}. La nueva versión reemplazará el contenido completo.`;$('revision-dialog').showModal();};actions.append(edit);head.append(info,actions);
      const body=node('div','reader-body');const preview=node('div','content-preview','Cargando versión…');const versions=node('aside','versions');versions.append(node('h3','','Historial'));
      for (const version of history.versions) {const button=node('button','version');button.type='button';button.dataset.revision=String(version.revision);button.append(node('strong','',`Versión ${version.revision}`),node('small','',`${formatTime(version.created_at)} · ${version.author_id || 'Autor no disponible'}`));button.onclick=()=>showVersion(item,version.revision,preview,versions);versions.append(button);}
      body.append(preview,versions);reader.replaceChildren(head,body);showVersion(item,item.revision,preview,versions);
    } catch(error){if(state.selected===id)reader.replaceChildren(node('div','loading',`No se pudo abrir el recurso: ${error.message}`));}
  }
  function dialog(id) { $(id).showModal(); }
  $('new-item').onclick=$('empty-create').onclick=()=>{$('item-form').reset();$('item-error').textContent='';dialog('item-dialog');};
  $('new-claim').onclick=()=>{$('claim-form').reset();$('claim-error').textContent='';dialog('claim-dialog');};
  document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
  $('search').addEventListener('input',renderList);
  $('item-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const data=await payload($('item-file'),$('item-content'));const result=await api('/context/items','POST',{title:$('item-title').value.trim(),path:$('item-path').value.trim(),...data});$('item-dialog').close();await refresh();await openItem(result.item.id);}catch(error){$('item-error').textContent=error.message;}finally{button.disabled=false;}};
  $('revision-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter,edit=state.edit;if(!edit)return;button.disabled=true;try{const data=await payload($('revision-file'),$('revision-content'));await api(`/context/items/${encodeURIComponent(edit.id)}/versions`,'POST',{expected_revision:edit.revision,...data});$('revision-dialog').close();await refresh();await openItem(edit.id);}catch(error){$('revision-error').textContent=error.status===409?'Otra persona actualizó este recurso. Cierra, abre la versión nueva y revisa los cambios antes de guardar.':error.message;}finally{button.disabled=false;}};
  $('jsonl-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter,edit=state.edit;if(!edit)return;button.disabled=true;try{await api(`/context/items/${encodeURIComponent(edit.id)}/append`,'POST',{expected_revision:edit.revision,record:{type:$('jsonl-type').value,summary:$('jsonl-summary').value.trim()}});$('jsonl-dialog').close();await refresh();await openItem(edit.id);}catch(error){$('jsonl-error').textContent=error.status===409?'El historial cambió mientras escribías. Cierra, recarga el recurso y vuelve a registrar el avance.':error.message;}finally{button.disabled=false;}};
  $('claim-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await api('/context/claims','POST',{path:$('claim-path').value.trim(),ttl_seconds:Number($('claim-ttl').value)});$('claim-dialog').close();await refresh();}catch(error){$('claim-error').textContent=error.status===409?'Esta ruta ya tiene una reserva activa.':error.message;}finally{button.disabled=false;}};
  api('/auth/session').then(session=>{state.accountId=session.user.id;return refresh();}).catch(error=>{$('global-status').textContent=error.message;});setInterval(refresh,15000);window.addEventListener('pagehide',clearPreview);
})();
