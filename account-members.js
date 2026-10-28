(() => {
  const list = document.getElementById('member-list');
  const count = document.getElementById('member-count');
  if (!list || !count) return;
  const fmt = value => value ? new Date(value * 1000).toLocaleDateString('es-CO', {day:'2-digit', month:'short', year:'numeric'}) : 'Sin acceso aún';
  async function loadMembers() {
    try {
      const response = await fetch('/account/members', {credentials:'same-origin', cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      list.replaceChildren(); count.textContent = `${data.count} usuario${data.count === 1 ? '' : 's'}`;
      for (const member of data.members) {
        const row = document.createElement('article'); row.className = 'member-row';
        const info = document.createElement('div'); info.innerHTML = `<strong>${member.username}</strong><small>${member.role || 'miembro'}${member.id === data.current_user_id ? ' · tú' : ''}</small>`;
        const state = document.createElement('span'); state.className = `member-state${member.disabled_at ? '' : ' active'}`; state.textContent = member.disabled_at ? 'Desactivado' : 'Activo';
        const time = document.createElement('time'); time.textContent = fmt(member.last_login_at);
        row.append(info, state, time); list.append(row);
      }
    } catch (error) { list.replaceChildren(Object.assign(document.createElement('div'), {className:'empty', textContent:'No se pudieron cargar los usuarios del workspace.'})); }
  }
  loadMembers();
})();
